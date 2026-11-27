"""Offline integration smoke test using a temporary database and saved reviews."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask
from collector.review_intelligence import install

root=Path(__file__).resolve().parents[1]
source=sqlite3.connect(f'file:{root / "data/collector.sqlite"}?mode=ro',uri=True)
source.row_factory=sqlite3.Row
rows=source.execute("SELECT * FROM reviews WHERE CAST(json_extract(data,'$.score') AS INTEGER) BETWEEN 1 AND 2 LIMIT 12").fetchall()
if not rows:
    raise SystemExit('Collect reviews before running the integration check.')
with tempfile.TemporaryDirectory() as temporary:
    def db():
        c=sqlite3.connect(Path(temporary)/'smoke.sqlite')
        c.row_factory=sqlite3.Row
        return c
    with db() as c:
        c.executescript('CREATE TABLE reviews(package TEXT,country TEXT,language TEXT,review_id TEXT,data TEXT); CREATE TABLE apps(package TEXT,metadata TEXT);')
        for row in rows:
            c.execute('INSERT INTO reviews VALUES(?,?,?,?,?)',tuple(row[k] for k in ('package','country','language','review_id','data')))
        for package in {r['package'] for r in rows}:
            metadata=source.execute('SELECT metadata FROM apps WHERE package=?',(package,)).fetchone()
            c.execute('INSERT INTO apps VALUES(?,?)',(package,metadata[0] if metadata else '{}'))
    app=Flask(__name__)
    worker=install(app,db)
    with db() as c:
        c.execute("INSERT INTO embedding_jobs(id,status) VALUES('smoke','queued')")
    worker('smoke',None)
    client=app.test_client()
    status=client.get('/api/intelligence').get_json()
    if status['jobs'][0]['status']!='completed':
        raise SystemExit(status['jobs'][0]['message'])
    print('Indexed actual saved reviews in temporary DB:',status['totals'])
    for query in ['withdrawal or payment problems','बहुत सारे विज्ञापन आते हैं','paise nahi mile']:
        response=client.post('/api/intelligence/search',json={'query':query,'max_stars':2,'limit':2})
        if response.status_code!=200:
            raise SystemExit(response.get_json())
        result=response.get_json()
        print(json.dumps({'query':query,'matches':[(r['title'],round(r['similarity'],3),r['review']['content'][:100]) for r in result['results']]},ensure_ascii=False))
source.close()
