"""Local Play Store collection workspace. Run with python -m collector.app."""
import csv
import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask, Response, jsonify, request, send_from_directory, send_file
from google_play_scraper import Sort, app as play_app, reviews, search
from google_play_scraper.utils import request as scraper_request
from collector.release_status import assess as assess_release, LABELS as RELEASE_LABELS
from collector.discovery import parse_relationships, section_links, expanded_apps

# The library otherwise leaves network reads unbounded.
scraper_request.urlopen = partial(scraper_request.urlopen, timeout=30)

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('COLLECTOR_DATA', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True)
(DATA / 'icons').mkdir(exist_ok=True)
app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config['MAX_CONTENT_LENGTH'] = 32768
lock = threading.Lock()
PACKAGE = re.compile(r'^[A-Za-z][\w]*(?:\.[A-Za-z][\w]*)+$')


def now():
    return datetime.now(timezone.utc).isoformat()


def review_totals(metadata):
    """Keep listing totals distinct from locally collected review records."""
    return dict(metadata, total_reviews=metadata.get('reviews'), total_ratings=metadata.get('ratings'), release=assess_release(metadata))


def db():
    conn = sqlite3.connect(DATA / 'collector.sqlite', timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS release_assessments(package TEXT PRIMARY KEY,status TEXT,note TEXT,source_url TEXT,observed_version TEXT,reviewed TEXT);
        CREATE TABLE IF NOT EXISTS apps (package TEXT PRIMARY KEY, metadata TEXT NOT NULL, collected TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_checks (package TEXT, relation TEXT, status TEXT, checked TEXT, PRIMARY KEY(package,relation));
        CREATE TABLE IF NOT EXISTS discovery_apps (package TEXT PRIMARY KEY, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, package TEXT, metadata TEXT, collected TEXT);
        CREATE TABLE IF NOT EXISTS review_checks (package TEXT,country TEXT,language TEXT,rating INTEGER,status TEXT,requested INTEGER,returned INTEGER,error TEXT,checked TEXT,PRIMARY KEY(package,country,language,rating));
        CREATE TABLE IF NOT EXISTS reviews (package TEXT, country TEXT, language TEXT, review_id TEXT, data TEXT, PRIMARY KEY(package,country,language,review_id));
        CREATE TABLE IF NOT EXISTS edges (package TEXT, source TEXT, relation TEXT, job TEXT, UNIQUE(package,source,relation,job));
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT, config TEXT, progress INTEGER DEFAULT 0, total INTEGER DEFAULT 0, collected INTEGER DEFAULT 0, created TEXT, finished TEXT);
        CREATE TABLE IF NOT EXISTS job_apps (job TEXT, package TEXT, source TEXT, relation TEXT, root_seed TEXT, root_app TEXT, depth INTEGER, status TEXT, acquisition TEXT, error TEXT, PRIMARY KEY(job,package));
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, job TEXT, level TEXT, message TEXT, created TEXT);
        ''')
        if 'reused' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
            c.execute('ALTER TABLE jobs ADD COLUMN reused INTEGER DEFAULT 0')
        c.execute("UPDATE jobs SET status='interrupted', finished=? WHERE status IN ('queued','running')", (now(),))
        c.execute("UPDATE job_apps SET status='interrupted' WHERE status IN ('queued','processing') AND job IN (SELECT id FROM jobs WHERE status='interrupted')")


def event(job, message, level='info'):
    with db() as c:
        c.execute('INSERT INTO events(job,level,message,created) VALUES(?,?,?,?)', (job, level, message, now()))


def retry(fn):
    for attempt in range(3):
        try:
            return fn()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def low_review_pages(job,package,cfg,rating,limit):
    """Bound each page and pace continuation requests for larger collections."""
    result=[]
    seen=set()
    continuation=None
    seen_tokens=set()
    while len(result)<limit:
        with db() as c:
            row=c.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()
        if row and row[0]=='stopping':
            return result,True
        kwargs=dict(lang=cfg['language'],country=cfg['country'],sort=Sort.NEWEST,count=min(200,limit-len(result)),filter_score_with=rating)
        if continuation is not None:
            kwargs['continuation_token']=continuation
        page,continuation=retry(lambda:reviews(package,**kwargs))
        before=len(result)
        for item in page:
            if item.get('score')==rating and item.get('reviewId') and item['reviewId'] not in seen:
                seen.add(item['reviewId'])
                result.append(item)
                if len(result)>=limit:
                    break
        token=getattr(continuation,'token',None)
        if not token or len(result)>=limit:
            break
        fingerprint=repr(token)
        if fingerprint in seen_tokens or len(result)==before:
            return result,True
        seen_tokens.add(fingerprint)
        time.sleep(1)
    return result,False


def collect_low_reviews(job,package,cfg,reuse):
    limit=cfg['review_limit']
    if not limit:
        return
    for rating in (1,2,3):
        with db() as c:
            previous=c.execute('SELECT * FROM review_checks WHERE package=? AND country=? AND language=? AND rating=?',(package,cfg['country'],cfg['language'],rating)).fetchone()
        if reuse and previous and previous['status'] in ('available','empty_returned') and previous['requested']>=limit:
            continue
        try:
            items,partial=low_review_pages(job,package,cfg,rating,limit)
            with db() as c:
                for item in items:
                    c.execute('INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)',(package,cfg['country'],cfg['language'],item['reviewId'],json.dumps(item,default=str)))
                c.execute('INSERT OR REPLACE INTO review_checks VALUES(?,?,?,?,?,?,?,?,?)',(package,cfg['country'],cfg['language'],rating,'partial' if partial else 'available' if items else 'empty_returned',limit,len(items),None,now()))
            event(job,f'{rating}-star reviews for {package}: {len(items)} returned'+(' (no higher-star fallback)' if not items else ''))
        except Exception as exc:
            with db() as c:
                c.execute('INSERT OR REPLACE INTO review_checks VALUES(?,?,?,?,?,?,?,?,?)',(package,cfg['country'],cfg['language'],rating,'failed',limit,0,type(exc).__name__,now()))
            event(job,f'{rating}-star review collection failed for {package}; evidence unavailable, not zero complaints.','warning')


def run_job(job, cfg):
    try:
        with db() as c:
            c.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
        queue, seen, contexts = [], set(), {}

        def enqueue(package, source, relation, depth=0):
            with db() as c:
                c.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?)', (package, source, relation, job))
            if package == source and relation in ('same_developer', 'similar_games'):
                return
            if PACKAGE.fullmatch(package) and package not in seen and len(queue) < cfg['max_apps']:
                seen.add(package)
                parent = contexts.get(source) if depth else None
                root_seed = parent['root_seed'] if parent else source
                root_app = parent['root_app'] if parent else package
                contexts[package] = dict(root_seed=root_seed, root_app=root_app)
                queue.append((package, depth))
                with db() as c:
                    c.execute('INSERT OR IGNORE INTO job_apps(job,package,source,relation,root_seed,root_app,depth,status,acquisition) VALUES(?,?,?,?,?,?,?,?,?)', (job, package, source, relation, root_seed, root_app, depth, 'queued', 'pending'))
                    c.execute('UPDATE jobs SET total=? WHERE id=?', (len(queue), job))

        for seed in cfg['seeds']:
            with db() as c:
                if c.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0] == 'stopping':
                    break
            parsed = urlparse(seed)
            package = parse_qs(parsed.query).get('id', [''])[0] if parsed.hostname == 'play.google.com' else seed
            if PACKAGE.fullmatch(package):
                enqueue(package, seed, 'seed')
            else:
                event(job, f'Searching “{seed}”')
                try:
                    results = retry(lambda: search(seed, lang=cfg['language'], country=cfg['country'], n_hits=cfg['results_per_keyword']))
                    for result in results:
                        enqueue(result['appId'], seed, 'keyword')
                    event(job, f'{len(results)} search matches for “{seed}”')
                    if not results:
                        event(job, f'No matches returned for “{seed}”; availability or search parsing may affect coverage.', 'warning')
                except Exception as exc:
                    event(job, f'Search failed for “{seed}”: {type(exc).__name__}: {exc}', 'error')
                time.sleep(1)
        index = 0
        while index < len(queue):
            with db() as c:
                status = c.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()['status']
            if status == 'stopping':
                break
            package, depth = queue[index]
            with db() as c:
                c.execute("UPDATE job_apps SET status='processing' WHERE job=? AND package=?", (job, package))
            event(job, f'Collecting {package}')
            try:
                with db() as c:
                    existing = c.execute('SELECT metadata FROM apps WHERE package=?', (package,)).fetchone()
                cached = json.loads(existing[0]) if existing else None
                reuse = cached is not None and not cfg.get('refresh_existing', False) and cached.get('country') == cfg['country'] and cached.get('language') == cfg['language']
                acquisition = 'reused' if reuse else ('refreshed' if cached else 'new')
                with db() as c:
                    c.execute('UPDATE job_apps SET acquisition=? WHERE job=? AND package=?', (acquisition, job, package))
                if reuse:
                    metadata = cached
                    with db() as c:
                        c.execute('UPDATE jobs SET reused=COALESCE(reused,0)+1 WHERE id=?', (job,))
                    event(job, f'Already collected: reusing {package}; new relationships are preserved')
                else:
                    metadata = retry(lambda: play_app(package, lang=cfg['language'], country=cfg['country']))
                    metadata.update(package_id=package, play_url=f'https://play.google.com/store/apps/details?id={package}', country=cfg['country'], language=cfg['language'])
                    icon = metadata.get('icon', '')
                    if urlparse(icon).hostname in ('play-lh.googleusercontent.com', 'lh3.googleusercontent.com'):
                        try:
                            image = requests.get(icon, timeout=20)
                            image.raise_for_status()
                            if image.headers.get('Content-Type', '').startswith('image/') and len(image.content) < 5_000_000:
                                (DATA / 'icons' / f'{package}.img').write_bytes(image.content)
                                metadata['local_icon'] = f'/api/icons/{package}'
                        except requests.RequestException:
                            event(job, f'Icon download failed for {package}', 'warning')
                    stamp = now()
                    with db() as c:
                        payload = json.dumps(metadata, default=str)
                        c.execute('INSERT OR REPLACE INTO apps VALUES(?,?,?)', (package, payload, stamp))
                        c.execute('INSERT INTO snapshots(package,metadata,collected) VALUES(?,?,?)', (package, payload, stamp))
                        c.execute('UPDATE jobs SET collected=collected+1 WHERE id=?', (job,))
                    if cfg['review_limit'] and cfg.get('review_mode') != 'low_only':
                        try:
                            items, _ = retry(lambda: reviews(package, lang=cfg['language'], country=cfg['country'], sort=Sort.NEWEST, count=cfg['review_limit']))
                            with db() as c:
                                for item in items:
                                    c.execute('INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)', (package, cfg['country'], cfg['language'], item['reviewId'], json.dumps(item, default=str)))
                            event(job, f'Saved {len(items)} reviews for {package}')
                        except Exception as exc:
                            event(job, f'Reviews failed for {package}: {type(exc).__name__}: {exc}', 'warning')
                if cfg.get('review_mode')=='low_only':
                    collect_low_reviews(job,package,cfg,reuse)
                if not reuse and cfg.get('review_mode')!='low_only' and cfg.get('low_star_limit', 0):
                    for rating in (1, 2):
                        try:
                            items, _ = retry(lambda rating=rating: reviews(package, lang=cfg['language'], country=cfg['country'], sort=Sort.NEWEST, count=cfg['low_star_limit'], filter_score_with=rating))
                            with db() as c:
                                for item in items:
                                    c.execute('INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)', (package, cfg['country'], cfg['language'], item['reviewId'], json.dumps(item, default=str)))
                            event(job, f'Saved {len(items)} targeted {rating}-star reviews for {package}')
                        except Exception as exc:
                            event(job, f'Targeted {rating}-star review collection failed for {package}: {type(exc).__name__}', 'warning')
                if depth < cfg['depth']:
                    if cfg['same_developer'] or cfg['related']:
                        try:
                            # English headings make section identity independent of review language.
                            response = retry(lambda: requests.get('https://play.google.com/store/apps/details', params={'id': package, 'hl': 'en', 'gl': cfg['country']}, timeout=30))
                            response.raise_for_status()
                            relationships, found = parse_relationships(response.text)
                            links = section_links(response.text)
                            for relation, enabled in [('same_developer', cfg['same_developer']), ('similar_games', cfg['related'])]:
                                if not enabled:
                                    continue
                                discovery_status = 'checked' if relation in found else 'unavailable'
                                if relation not in found:
                                    event(job, f'{relation} section unavailable for {package}; discovery coverage is incomplete', 'warning')
                                candidates = [candidate for candidate in relationships[relation] if candidate != package]
                                if relation in links:
                                    try:
                                        expansion = expanded_apps(links[relation], cfg['country'], cfg['language'], cfg['max_apps'])
                                        with db() as c:
                                            for item in expansion['apps']:
                                                if PACKAGE.fullmatch(item.get('appId', '')):
                                                    c.execute('INSERT OR REPLACE INTO discovery_apps VALUES(?,?)', (item['appId'], json.dumps(item)))
                                        candidates = list(dict.fromkeys(candidates + [a['appId'] for a in expansion['apps'] if a.get('appId') != package]))
                                        discovery_status = 'limited' if expansion['limit_reached'] else 'checked'
                                        event(job, f'{relation}: followed See more, found {len(candidates)} candidates' + ('; discovery limit reached' if expansion['limit_reached'] else ''))
                                    except Exception as exc:
                                        discovery_status = 'partial'
                                        event(job, f'{relation}: See more pagination failed; retaining visible candidates: {exc}', 'warning')
                                for candidate in candidates:
                                    enqueue(candidate, package, relation, depth + 1)
                                with db() as c:
                                    c.execute('INSERT OR REPLACE INTO discovery_checks VALUES(?,?,?,?)', (package, relation, discovery_status, now()))
                                event(job, f'{len(candidates)} {relation} candidates for {package}')
                        except Exception as exc:
                            event(job, f'Section discovery failed for {package}: {exc}', 'warning')
                with db() as c:
                    c.execute("UPDATE job_apps SET status='completed' WHERE job=? AND package=?", (job, package))
                event(job, f'{"Reused" if reuse else "Saved"} {metadata.get("title", package)}', 'success')
            except Exception as exc:
                with db() as c:
                    c.execute("UPDATE job_apps SET status='failed',error=? WHERE job=? AND package=?", (str(exc), job, package))
                event(job, f'Collection failed for {package}: {type(exc).__name__}: {exc}', 'error')
            index += 1
            with db() as c:
                c.execute('UPDATE jobs SET progress=?,total=? WHERE id=?', (index, len(queue), job))
            time.sleep(1)
        with db() as c:
            errors = c.execute("SELECT COUNT(*) FROM events WHERE job=? AND level IN ('error','warning')", (job,)).fetchone()[0]
            status = c.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0]
            final = 'cancelled' if status == 'stopping' else ('completed_with_warnings' if errors else 'completed')
            c.execute("UPDATE job_apps SET status='cancelled' WHERE job=? AND status='queued'", (job,))
            c.execute('UPDATE jobs SET status=?,finished=?,total=? WHERE id=?', (final, now(), len(queue), job))
        event(job, 'Collection finished' if final != 'cancelled' else 'Collection stopped')
    except Exception as exc:
        event(job, f'Job failed: {type(exc).__name__}: {exc}', 'error')
        with db() as c:
            c.execute("UPDATE jobs SET status='failed',finished=? WHERE id=?", (now(), job))
            c.execute("UPDATE job_apps SET status='interrupted' WHERE job=? AND status IN ('queued','processing')", (job,))


@app.before_request
def local_requests():
    if request.host.split(':')[0] not in ('localhost', '127.0.0.1'):
        return jsonify(error='Local access only'), 403
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            return jsonify(error='Cross-origin writes are not allowed'), 403
        if not request.is_json:
            return jsonify(error='JSON body required'), 415


@app.get('/')
def home():
    return app.send_static_file('index.html')


@app.get('/api/state')
def state():
    with db() as c:
        apps = []
        for row in c.execute('SELECT * FROM apps ORDER BY collected DESC'):
            item = review_totals(json.loads(row['metadata']))
            item.setdefault('appId',row['package'])
            release_review=c.execute('SELECT * FROM release_assessments WHERE package=?',(row['package'],)).fetchone()
            item['release']=assess_release(item,release_review)
            item['collected_at'] = row['collected']
            item['saved_reviews'] = c.execute('SELECT COUNT(*) FROM reviews WHERE package=?', (row['package'],)).fetchone()[0]
            item['low_star_reviews'] = c.execute("SELECT COUNT(*) FROM reviews WHERE package=? AND CAST(json_extract(data,'$.score') AS INTEGER) BETWEEN 1 AND 3", (row['package'],)).fetchone()[0]
            apps.append(item)
        jobs = [dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 30')]
        for job in jobs:
            job['apps'] = []
            for row in c.execute('SELECT ja.*,a.metadata,d.metadata AS discovery_metadata FROM job_apps ja LEFT JOIN apps a ON a.package=ja.package LEFT JOIN discovery_apps d ON d.package=ja.package WHERE ja.job=? ORDER BY ja.rowid', (job['id'],)):
                item = dict(row)
                metadata = json.loads(item.pop('metadata') or item.pop('discovery_metadata') or '{}')
                item.pop('discovery_metadata', None)
                item['title'] = metadata.get('title', item['package'])
                job['apps'].append(item)
        events = [dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]
        count = c.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]
    return jsonify(apps=apps, jobs=jobs, events=events, review_count=count)


@app.post('/api/jobs')
def create_job():
    data = request.get_json()
    try:
        seeds = data.get('seeds', [])
        if not isinstance(seeds, list) or not 1 <= len(seeds) <= 20 or any(not isinstance(s, str) or not s.strip() or len(s) > 300 for s in seeds):
            raise ValueError('Enter 1–20 keywords, package IDs, or Play Store URLs.')
        cfg = dict(seeds=[s.strip() for s in seeds], country=str(data.get('country', 'in')).lower(), language=str(data.get('language', 'en')).lower(),
                   results_per_keyword=int(data.get('results_per_keyword', 10)), max_apps=int(data.get('max_apps', 30)),
                   review_mode='low_only', review_limit=int(data.get('review_limit', 50)), low_star_limit=int(data.get('low_star_limit', 0)), depth=int(data.get('depth', 2)), refresh_existing=bool(data.get('refresh_existing', False)),
                   same_developer=bool(data.get('same_developer', True)), related=bool(data.get('related', True)))
        if not re.fullmatch('[a-z]{2}', cfg['country']) or not re.fullmatch('[a-z]{2}', cfg['language']):
            raise ValueError('Use two-letter country and language codes.')
        for field, low, high in [('results_per_keyword', 1, 50), ('max_apps', 1, 1000), ('review_limit', 0, 10000), ('low_star_limit', 0, 500), ('depth', 0, 5)]:
            if not low <= cfg[field] <= high:
                raise ValueError(f'{field} must be between {low} and {high}.')
    except (ValueError, TypeError, AttributeError) as exc:
        return jsonify(error=str(exc)), 400
    with lock, db() as c:
        if c.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','stopping')").fetchone():
            return jsonify(error='A collection is already running. Stop it or wait for completion.'), 409
        job = uuid.uuid4().hex
        c.execute('INSERT INTO jobs(id,status,config,created) VALUES(?,?,?,?)', (job, 'queued', json.dumps(cfg), now()))
    threading.Thread(target=run_job, args=(job, cfg), daemon=True).start()
    return jsonify(id=job), 202


@app.post('/api/jobs/<job>/stop')
def stop(job):
    with db() as c:
        c.execute("UPDATE jobs SET status='stopping' WHERE id=? AND status IN ('queued','running')", (job,))
    return jsonify(ok=True)


@app.get('/api/apps/<package>')
def detail(package):
    with db() as c:
        row = c.execute('SELECT metadata FROM apps WHERE package=?', (package,)).fetchone()
        if not row:
            return jsonify(error='App not found'), 404
        connections = {'same_developer': [], 'similar_games': []}
        for edge in c.execute("SELECT DISTINCT e.package,e.relation,a.metadata,d.metadata AS discovery_metadata FROM edges e LEFT JOIN apps a ON a.package=e.package LEFT JOIN discovery_apps d ON d.package=e.package WHERE e.source=? AND e.relation IN ('same_developer','similar_games') ORDER BY e.package", (package,)):
            metadata = json.loads(edge['metadata'] or edge['discovery_metadata'] or '{}')
            connections[edge['relation']].append(dict(package_id=edge['package'], title=metadata.get('title', edge['package']), developer=metadata.get('developer'), local_icon=metadata.get('local_icon'), collected=edge['metadata'] is not None, play_url=f"https://play.google.com/store/apps/details?id={edge['package']}"))
        discovery_status = {relation: {'status': 'recorded' if items else 'not_explored'} for relation, items in connections.items()}
        for check in c.execute('SELECT relation,status,checked FROM discovery_checks WHERE package=?', (package,)):
            discovery_status[check['relation']] = dict(check)
        release_review=c.execute('SELECT * FROM release_assessments WHERE package=?',(package,)).fetchone()
        details=review_totals(json.loads(row[0]))
        details.setdefault('appId',package)
        details['release']=assess_release(details,release_review)
        return jsonify(review_counts={str(r[0]):r[1] for r in c.execute("SELECT CAST(json_extract(data, '$.score') AS INTEGER),COUNT(*) FROM reviews WHERE package=? GROUP BY CAST(json_extract(data, '$.score') AS INTEGER)",(package,))}, review_checks=[dict(r) for r in c.execute('SELECT * FROM review_checks WHERE package=? ORDER BY country,language,rating',(package,))], low_star_count=c.execute("SELECT COUNT(*) FROM reviews WHERE package=? AND CAST(json_extract(data, '$.score') AS INTEGER) BETWEEN 1 AND 3", (package,)).fetchone()[0], discovery_status=discovery_status, connections=connections, metadata=details, review_count=c.execute('SELECT COUNT(*) FROM reviews WHERE package=?', (package,)).fetchone()[0], reviews=[json.loads(r[0]) for r in c.execute("SELECT data FROM reviews WHERE package=? ORDER BY json_extract(data, '$.at') DESC LIMIT 1000", (package,))], sources=[dict(r) for r in c.execute('SELECT source,relation,job FROM edges WHERE package=?', (package,))])


@app.post('/api/apps/<package>/release-status')
def set_release_status(package):
    body=request.get_json()
    if not isinstance(body,dict) or body.get('status') not in RELEASE_LABELS:
        return jsonify(error='Choose a valid testing status.'),400
    status=body['status']
    note=body.get('note','')
    source=body.get('source_url','')
    version=body.get('observed_version','')
    if not all(isinstance(v,str) for v in (note,source,version)) or len(note)>2000 or len(source)>1000 or len(version)>100:
        return jsonify(error='Evidence fields are invalid or too long.'),400
    parsed=urlparse(source)
    if source and (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password):
        return jsonify(error='Use a public HTTPS evidence URL without credentials.'),400
    if status in ('confirmed_beta','confirmed_testing','beta_program_available') and (len(note.strip())<10 or not source):
        return jsonify(error='Record an evidence URL and a description of what you verified. A title/version hint is insufficient.'),400
    with db() as c:
        if not c.execute('SELECT 1 FROM apps WHERE package=?',(package,)).fetchone():
            return jsonify(error='App not found.'),404
        if status=='unknown':
            c.execute('DELETE FROM release_assessments WHERE package=?',(package,))
        else:
            c.execute('INSERT OR REPLACE INTO release_assessments VALUES(?,?,?,?,?,?)',(package,status,note.strip(),source,version,now()))
    return jsonify(ok=True)


@app.get('/api/icons/<package>')
def icon(package):
    if not PACKAGE.fullmatch(package):
        return '', 404
    return send_from_directory(DATA / 'icons', f'{package}.img', mimetype='image/png')


@app.get('/api/export')
def export():
    if request.args.get('format')=='xlsx':
        from collector.excel_export import build_workbook
        return send_file(build_workbook(db),mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',as_attachment=True,download_name='AppTrace-collection.xlsx')
    with db() as c:
        apps = []
        for row in c.execute('SELECT package,metadata FROM apps'):
            item = review_totals(json.loads(row['metadata']))
            item.setdefault('appId',row['package'])
            release_review=c.execute('SELECT * FROM release_assessments WHERE package=?',(row['package'],)).fetchone()
            item['release']=assess_release(item,release_review)
            item['saved_reviews'] = c.execute('SELECT COUNT(*) FROM reviews WHERE package=?', (row['package'],)).fetchone()[0]
            apps.append(item)
        if request.args.get('format') == 'csv':
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=['appId', 'title', 'developer', 'developerId', 'genre', 'score', 'installs', 'version', 'play_url', 'total_reviews', 'total_ratings', 'saved_reviews'])
            writer.writeheader()
            for item in apps:
                writer.writerow({k: ("'" + str(item.get(k, '')) if str(item.get(k, '')).startswith(('=', '+', '-', '@')) else item.get(k, '')) for k in writer.fieldnames})
            return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=play-collection.csv'})
        payload = dict(release_assessments=[dict(r) for r in c.execute('SELECT * FROM release_assessments')], review_checks=[dict(r) for r in c.execute('SELECT * FROM review_checks')], apk_downloads=[dict(r) for r in c.execute('SELECT * FROM apk_downloads')], apk_files=[dict(r) for r in c.execute('SELECT * FROM apk_files')], apps=apps, discovered_apps=[dict(r) for r in c.execute('SELECT * FROM discovery_apps')], job_apps=[dict(r) for r in c.execute('SELECT * FROM job_apps')], reviews=[dict(r) for r in c.execute('SELECT * FROM reviews')], relationships=[dict(r) for r in c.execute('SELECT * FROM edges')], snapshots=[dict(r) for r in c.execute('SELECT * FROM snapshots')], jobs=[dict(r) for r in c.execute('SELECT * FROM jobs')], events=[dict(r) for r in c.execute('SELECT * FROM events')])
    return Response(json.dumps(payload, default=str), mimetype='application/json', headers={'Content-Disposition': 'attachment; filename=play-collection.json'})


init_db()
from collector.review_intelligence import install as install_intelligence
embedding_worker = install_intelligence(app, db)
from collector.apks import install as install_apks
apk_worker = install_apks(app, db, DATA)
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', 5050)), debug=False)
