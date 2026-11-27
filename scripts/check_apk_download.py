"""Live one-app download check in a temporary database, using local Play auth."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask
from collector.apks import install, stamp

if len(sys.argv)!=2:
    raise SystemExit('Usage: .venv/bin/python scripts/check_apk_download.py PACKAGE_ID')
package=sys.argv[1]
root=Path(__file__).resolve().parents[1]
source=sqlite3.connect(f'file:{root / "data/collector.sqlite"}?mode=ro',uri=True)
row=source.execute('SELECT metadata FROM apps WHERE package=?',(package,)).fetchone()
source.close()
if not row:
    raise SystemExit('Choose an app already saved in the library.')
with tempfile.TemporaryDirectory(prefix='apptrace-apk-check-') as temporary:
    data=Path(temporary)
    def db():
        c=sqlite3.connect(data/'collector.sqlite')
        c.row_factory=sqlite3.Row
        return c
    with db() as c:
        c.execute('CREATE TABLE apps(package TEXT PRIMARY KEY,metadata TEXT)')
        c.execute('INSERT INTO apps VALUES(?,?)',(package,row[0]))
    app=Flask(__name__)
    worker=install(app,db,data)
    with db() as c:
        c.execute("INSERT INTO apk_batches(id,status,architecture,created,finished) VALUES('smoke','queued','arm64',?,NULL)",(stamp(),))
        c.execute("INSERT INTO apk_downloads(id,batch,package,architecture,status,created) VALUES('smoke','smoke',?,'arm64','queued',?)",(package,stamp()))
    print('Checking one live APK download:',package,flush=True)
    worker('smoke')
    result=app.test_client().get('/api/apks').get_json()['downloads'][0]
    print(json.dumps({k:result[k] for k in ('package','status','version_code','version_name','error','files')},indent=2))
    if result['status']!='downloaded':
        raise SystemExit(1)
