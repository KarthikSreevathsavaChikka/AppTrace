"""Background review indexing and semantic search endpoints."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from collector import embeddings


def stamp():
    return datetime.now(timezone.utc).isoformat()


def install(app, db):
    api = Blueprint('intelligence', __name__)
    queue_lock = threading.Lock()
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS embeddings (text_hash TEXT, model_key TEXT, vector BLOB NOT NULL, dimensions INTEGER NOT NULL, created TEXT, PRIMARY KEY(text_hash,model_key));
        CREATE TABLE IF NOT EXISTS review_vectors (package TEXT, country TEXT, language TEXT, review_id TEXT, model_key TEXT, text_hash TEXT, PRIMARY KEY(package,country,language,review_id,model_key));
        CREATE TABLE IF NOT EXISTS embedding_jobs (id TEXT PRIMARY KEY, status TEXT, scope TEXT, total INTEGER DEFAULT 0, processed INTEGER DEFAULT 0, generated INTEGER DEFAULT 0, reused INTEGER DEFAULT 0, message TEXT, created TEXT, finished TEXT);
        ''')
        c.execute("UPDATE embedding_jobs SET status='interrupted',message='Server restarted; start indexing again to resume from cached vectors.',finished=? WHERE status IN ('queued','running','stopping')", (stamp(),))

    def records(package=None):
        with db() as c:
            sql = 'SELECT * FROM reviews' + (' WHERE package=?' if package else '')
            rows = c.execute(sql, (package,) if package else ()).fetchall()
        items = []
        for row in rows:
            review = json.loads(row['data'])
            text = embeddings.text_content(review)
            if text and 1 <= (review.get('score') or 0) <= 3:
                items.append(dict(row, review=review, text=text, hash=embeddings.text_hash(text)))
        return sorted(items, key=lambda r: (0 if 1 <= (r['review'].get('score') or 0) <= 3 else 1, r['package']))

    def cache():
        with db() as c:
            return {r['text_hash'] for r in c.execute('SELECT text_hash FROM embeddings WHERE model_key=?', (embeddings.model_key(),))}

    def mapped():
        with db() as c:
            return {(r['package'], r['country'], r['language'], r['review_id']): r['text_hash'] for r in c.execute('SELECT * FROM review_vectors WHERE model_key=?', (embeddings.model_key(),))}

    def worker(job, package):
        try:
            items = records(package)
            with db() as c:
                if c.execute('SELECT status FROM embedding_jobs WHERE id=?', (job,)).fetchone()[0] == 'stopping':
                    c.execute("UPDATE embedding_jobs SET status='cancelled',finished=? WHERE id=?", (stamp(),job))
                    return
                c.execute("UPDATE embedding_jobs SET status='running',total=?,message='Preparing local model; first load can take a minute.' WHERE id=?", (len(items), job))
            known = cache()
            for start in range(0, len(items), 16):
                with db() as c:
                    if c.execute('SELECT status FROM embedding_jobs WHERE id=?', (job,)).fetchone()[0] == 'stopping':
                        c.execute("UPDATE embedding_jobs SET status='cancelled',finished=? WHERE id=?", (stamp(), job))
                        return
                batch = items[start:start + 16]
                pending = {r['hash']: r['text'] for r in batch if r['hash'] not in known}
                generated = {}
                if pending:
                    vectors = embeddings.encode(list(pending.values()))
                    generated = dict(zip(pending, vectors))
                with db() as c:
                    for digest, vector in generated.items():
                        c.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?)', (digest, embeddings.model_key(), vector.tobytes(), len(vector), stamp()))
                    for row in batch:
                        c.execute('INSERT OR REPLACE INTO review_vectors VALUES(?,?,?,?,?,?)', (row['package'], row['country'], row['language'], row['review_id'], embeddings.model_key(), row['hash']))
                    c.execute('UPDATE embedding_jobs SET processed=processed+?,generated=generated+?,reused=reused+?,message=? WHERE id=?',
                              (len(batch), len(generated), len(batch)-len(generated), f'Indexed {min(start+16,len(items))} of {len(items)} reviews; low-star reviews first.', job))
                known.update(generated)
            with db() as c:
                c.execute("UPDATE embedding_jobs SET status='completed',message='Indexing complete.',finished=? WHERE id=?", (stamp(), job))
        except Exception as exc:
            # Never return download URLs, credentials, or a raw model-loader traceback.
            message = 'Embedding model could not run. Prepare the pinned model with .venv/bin/python scripts/prepare_embeddings.py, then retry.'
            if isinstance(exc, sqlite3.Error):
                message = 'Database write failed; check disk space and restart indexing.'
            with db() as c:
                c.execute("UPDATE embedding_jobs SET status='failed',message=?,finished=? WHERE id=?", (message, stamp(), job))

    @api.get('/api/intelligence')
    def status():
        items, known, links = records(), cache(), mapped()
        coverage = {}
        for r in items:
            entry = coverage.setdefault(r['package'], dict(total=0, indexed=0, low_star=0, low_star_indexed=0))
            entry['total'] += 1
            low = 1 <= (r['review'].get('score') or 0) <= 3
            valid = r['hash'] in known and links.get((r['package'],r['country'],r['language'],r['review_id'])) == r['hash']
            entry['indexed'] += int(valid)
            entry['low_star'] += int(low)
            entry['low_star_indexed'] += int(valid and low)
        with db() as c:
            jobs = [dict(r) for r in c.execute('SELECT * FROM embedding_jobs ORDER BY created DESC LIMIT 10')]
        totals = {k:sum(v[k] for v in coverage.values()) for k in ('total','indexed','low_star','low_star_indexed')}
        cfg = embeddings.spec()
        return jsonify(model=cfg['model_id'], revision=cfg['revision'], dimensions=cfg['dimensions'], coverage=coverage, totals=totals, jobs=jobs)

    @api.post('/api/intelligence/index')
    def index():
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error='JSON object required'), 400
        package = data.get('package')
        if package is not None:
            if not isinstance(package, str):
                return jsonify(error='Invalid app package'), 400
            with db() as c:
                if not c.execute('SELECT 1 FROM apps WHERE package=?', (package,)).fetchone():
                    return jsonify(error='App not found'), 404
        with queue_lock, db() as c:
            if c.execute("SELECT 1 FROM embedding_jobs WHERE status IN ('queued','running','stopping')").fetchone():
                return jsonify(error='Review indexing is already running.'), 409
            job = uuid.uuid4().hex
            c.execute('INSERT INTO embedding_jobs(id,status,scope,created) VALUES(?,?,?,?)', (job,'queued',package or 'all',stamp()))
        threading.Thread(target=worker,args=(job,package),daemon=True).start()
        return jsonify(id=job), 202

    @api.post('/api/intelligence/jobs/<job>/stop')
    def stop(job):
        with db() as c:
            c.execute("UPDATE embedding_jobs SET status='stopping' WHERE id=? AND status IN ('queued','running')", (job,))
        return jsonify(ok=True)

    @api.post('/api/intelligence/search')
    def search():
        import numpy as np
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error='JSON object required'), 400
        query = data.get('query', '')
        package = data.get('package') or None
        try:
            max_stars = int(data.get('max_stars', 3))
            limit = None if data.get('limit') == 'all' else int(data.get('limit', 20))
        except (ValueError, TypeError):
            return jsonify(error='Invalid search filters'), 400
        if not isinstance(query,str) or not query.strip() or len(query)>1000 or max_stars not in (1,2,3) or (limit is not None and not 1<=limit<=100) or (package is not None and not isinstance(package,str)):
            return jsonify(error='Enter a query of 1–1000 characters and valid search filters.'), 400
        with db() as c:
            rows = c.execute('''SELECT r.*,v.text_hash,e.vector,e.dimensions,a.metadata FROM reviews r
                JOIN review_vectors v ON r.package=v.package AND r.country=v.country AND r.language=v.language AND r.review_id=v.review_id
                JOIN embeddings e ON v.text_hash=e.text_hash AND v.model_key=e.model_key
                LEFT JOIN apps a ON r.package=a.package WHERE v.model_key=?''', (embeddings.model_key(),)).fetchall()
        candidates = []
        for row in rows:
            review=json.loads(row['data'])
            if (package and row['package']!=package) or not 1 <= (review.get('score') or 0) <= max_stars:
                continue
            if embeddings.text_hash(embeddings.text_content(review)) != row['text_hash']:
                continue
            candidates.append((row,review))
        if not candidates:
            return jsonify(results=[], indexed_candidates=0, message='No indexed reviews match these filters. Index reviews first or change the filters.')
        try:
            vector = embeddings.encode([query.strip()], query=True)[0]
        except Exception:
            return jsonify(error='Local model is unavailable. Prepare the model and retry.'), 503
        ranked=[]
        for row,review in candidates:
            values=np.frombuffer(row['vector'],dtype='<f4')
            if values.shape != vector.shape:
                continue
            score=float(np.dot(vector,values))
            if not np.isfinite(score):
                continue
            metadata=json.loads(row['metadata'] or '{}')
            ranked.append(dict(package=row['package'],title=metadata.get('title',row['package']),country=row['country'],language=row['language'],review=review,similarity=score))
        ranked.sort(key=lambda r:r['similarity'],reverse=True)
        unique=[]
        seen=set()
        for item in ranked:
            key=(item['package'],item['review'].get('reviewId'),item['review'].get('content'))
            if key not in seen:
                unique.append(item)
                seen.add(key)
        return jsonify(results=unique[:limit],indexed_candidates=len(candidates),model=embeddings.model_key(),message='Similarity ranks relevance; it is not a risk or confidence score.')

    app.register_blueprint(api)
    return worker
