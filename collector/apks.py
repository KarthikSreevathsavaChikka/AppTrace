"""Original APK acquisition with manifest-verified database associations."""
import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import signal
import time
import random
import sys
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, send_file

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = re.compile(r'^[A-Za-z][\w]*(?:\.[A-Za-z][\w]*)+$')
ANDROID = '{http://schemas.android.com/apk/res/android}'
PROCESSES=set()
PROCESS_LOCK=threading.Lock()


def shutdown_downloaders():
    with PROCESS_LOCK:
        children=list(PROCESSES)
    for child in children:
        if child.poll() is None:
            try:
                os.killpg(child.pid,signal.SIGTERM)
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass


atexit.register(shutdown_downloaders)



def stamp():
    return datetime.now(timezone.utc).isoformat()


def upstream():
    pin = json.loads((ROOT / 'upstream.json').read_text())
    checkout = ROOT / pin['checkout']
    revision = subprocess.check_output(['git','-C',str(checkout),'rev-parse','HEAD'],text=True).strip()
    if revision != pin['commit']:
        raise ValueError('Downloader checkout differs from the pinned revision.')
    return checkout,pin['commit']


def auth_ready(arch):
    path = Path.home() / ('.gplay-auth-armv7.json' if arch == 'armv7' else '.gplay-auth.json')
    try:
        return bool(json.loads(path.read_text()).get('authToken'))
    except (OSError, ValueError, AttributeError):
        return False


class SharedDownloadError(ValueError):
    """A session/environment problem that should stop the whole batch."""


def download_error(output):
    """Classify known failures without storing raw logs or credentials."""
    if re.search(r'(?:details|URL|download|acquire app):?\s*(?:HTTP\s*)?401\b|HTTP\s*401', output, re.I):
        return SharedDownloadError('Google Play rejected the saved session (HTTP 401). Refresh the local Play session before retrying.')
    if any(tag in output for tag in ('NameResolutionError', 'Could not resolve', 'Failed to resolve', 'Name or service not known', 'Network is unreachable', 'Connection refused')):
        return SharedDownloadError('The downloader cannot reach Google Play. Check network/DNS access before retrying.')
    if any(tag in output for tag in ('ModuleNotFoundError', 'ImportError', 'library required', 'library not found')):
        return SharedDownloadError('Downloader dependencies are unavailable in the server Python environment. Start AppTrace using .venv/bin/python and install the upstream requirements.')
    if '403' in output:
        return ValueError('Google Play denied this request (HTTP 403). Check account access, region, and device compatibility.')
    if '404' in output or 'App not found' in output:
        return ValueError('App is unavailable for this account, region, or device (not found).')
    if 'paid app' in output.lower() or 'purchase required' in output.lower():
        return ValueError('This app requires purchase; it was not downloaded.')
    return ValueError('Play download failed. Check app availability, account region, and device compatibility.')


class CancelledDownload(ValueError):
    pass


class TransportError(ValueError):
    def __init__(self,kind,message,retry_after=None):
        super().__init__(message)
        self.kind=kind
        self.retry_after=retry_after


def transport(mode,package,arch,directory=None,version=None,should_cancel=lambda:False):
    _,revision=upstream()
    command=[sys.executable,str(ROOT/'scripts/apk_transport.py'),mode,package,'--arch',arch]
    if directory is not None:
        command+=['--output',str(directory)]
    if version is not None:
        command+=['--version',str(version)]
    process=subprocess.Popen(command,env=dict(os.environ,PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION='python'),
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    with PROCESS_LOCK:
        PROCESSES.add(process)
    started=time.monotonic()
    try:
        while True:
            if should_cancel():
                raise CancelledDownload('Download cancelled by user.')
            if time.monotonic()-started>1800:
                raise TransportError('transient','Download exceeded the 30-minute time limit.')
            try:
                output,error=process.communicate(timeout=.5)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
        with PROCESS_LOCK:
            PROCESSES.discard(process)
    if process.returncode:
        for line in reversed(error.splitlines()):
            if line.startswith('APPTRACE_ERROR '):
                detail=json.loads(line[len('APPTRACE_ERROR '):])
                raise TransportError(detail['kind'],detail['message'],detail.get('retry_after'))
        raise download_error(output+'\n'+error)
    return (json.loads(output) if mode=='probe' else revision)


def available_version(package,arch,should_cancel=lambda:False):
    return transport('probe',package,arch,should_cancel=should_cancel)


def acquire(package,arch,directory,should_cancel=lambda:False,version=None):
    return transport('download',package,arch,directory,version,should_cancel)


def inspect_manifest(path):
    from pyaxmlparser.axmlprinter import AXMLPrinter
    with zipfile.ZipFile(path) as archive:
        manifests = [i for i in archive.infolist() if i.filename == 'AndroidManifest.xml']
        if len(manifests) != 1 or manifests[0].file_size > 10_000_000:
            raise ValueError('Missing, duplicate, or oversized Android manifest.')
        if archive.testzip() is not None:
            raise ValueError('APK ZIP integrity validation failed.')
        xml = AXMLPrinter(archive.read(manifests[0])).get_xml_obj()
    if xml is None or xml.tag != 'manifest':
        raise ValueError('Could not parse APK manifest.')
    version = xml.get(ANDROID+'versionCode')
    if version is None:
        raise ValueError('APK manifest has no version code.')
    code = int(version,16) if version.startswith('0x') else int(version)
    major = xml.get(ANDROID+'versionCodeMajor','0')
    code += (int(major,16) if major.startswith('0x') else int(major)) << 32
    return dict(package=xml.get('package'),version_code=code,version_name=xml.get(ANDROID+'versionName'),split_name=xml.get('split') or None)


def validate_set(directory, package):
    files=[]
    for path in sorted(directory.glob('*.apk')):
        manifest=inspect_manifest(path)
        if manifest['package'] != package:
            raise ValueError('Downloaded APK package does not match the selected app.')
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                digest.update(chunk)
        files.append(dict(manifest,path=path,size_bytes=path.stat().st_size,sha256=digest.hexdigest()))
    if not files or sum(f['split_name'] is None for f in files)!=1:
        raise ValueError('Download must contain exactly one base APK.')
    if len({f['version_code'] for f in files}) != 1:
        raise ValueError('Base and split APK versions do not match.')
    if len({f['split_name'] for f in files}) != len(files):
        raise ValueError('Download contains duplicate split identities.')
    return files


def paced_wait(seconds,should_cancel):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if should_cancel():
            raise CancelledDownload('Download cancelled by user.')
        time.sleep(min(.5,max(0,deadline-time.monotonic())))


def install(app,db,data):
    data=Path(data).resolve()
    api=Blueprint('apks',__name__)
    queue_lock=threading.Lock()
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS apk_batches(id TEXT PRIMARY KEY,status TEXT,architecture TEXT,created TEXT,finished TEXT);
        CREATE TABLE IF NOT EXISTS apk_downloads(id TEXT PRIMARY KEY,batch TEXT,package TEXT,architecture TEXT,status TEXT,version_code INTEGER,version_name TEXT,error TEXT,upstream_commit TEXT,created TEXT,finished TEXT,FOREIGN KEY(package) REFERENCES apps(package));
        CREATE TABLE IF NOT EXISTS apk_files(id TEXT PRIMARY KEY,download_id TEXT,relative_path TEXT,kind TEXT,split_name TEXT,size_bytes INTEGER,sha256 TEXT,FOREIGN KEY(download_id) REFERENCES apk_downloads(id));
        CREATE INDEX IF NOT EXISTS apk_file_download_idx ON apk_files(download_id);
        CREATE INDEX IF NOT EXISTS apk_package_idx ON apk_downloads(package);
        ''')
        migrations={
            'apk_batches':{'pause_seconds':'INTEGER DEFAULT 10','chunk_size':'INTEGER DEFAULT 10','chunk_pause':'INTEGER DEFAULT 60','max_retries':'INTEGER DEFAULT 2','message':'TEXT','resume_after':'REAL'},
            'apk_downloads':{'attempts':'INTEGER DEFAULT 0','cancel_requested':'INTEGER DEFAULT 0','cancelled_by':'TEXT','existing_download_id':'TEXT'}}
        for table,columns in migrations.items():
            present={r[1] for r in c.execute(f'PRAGMA table_info({table})')}
            for column,definition in columns.items():
                if column not in present:
                    c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        c.execute("UPDATE apk_downloads SET status='paused',error='Server restarted; resume the batch to restart this unfinished app.',cancel_requested=0 WHERE status='downloading'")
        c.execute("UPDATE apk_batches SET status='paused',message='Server restarted. Completed files were preserved; resume unfinished apps.' WHERE status IN ('queued','running','stopping')")

    def cancelled(batch,identifier=None):
        with db() as c:
            status=c.execute('SELECT status FROM apk_batches WHERE id=?',(batch,)).fetchone()
            if not status or status[0] not in ('queued','running'):
                return True
            if identifier:
                row=c.execute('SELECT cancel_requested,status FROM apk_downloads WHERE id=?',(identifier,)).fetchone()
                return not row or row[0] or row[1]=='cancelled'
        return False

    def wait(seconds,batch,identifier=None):
        paced_wait(seconds,lambda:cancelled(batch,identifier))

    def cached(package,arch,version):
        with db() as c:
            rows=c.execute("SELECT * FROM apk_downloads WHERE package=? AND architecture=? AND version_code=? AND status='downloaded' ORDER BY created DESC",(package,arch,version)).fetchall()
            for row in rows:
                files=c.execute('SELECT * FROM apk_files WHERE download_id=?',(row['id'],)).fetchall()
                if not files or sum(f['kind']=='base' for f in files)!=1:
                    continue
                valid=True
                for file in files:
                    path=(data/file['relative_path']).resolve()
                    if not path.is_relative_to(data/'apks') or not path.is_file() or path.stat().st_size!=file['size_bytes']:
                        valid=False
                        break
                    digest=hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda:stream.read(1048576),b''):
                            digest.update(chunk)
                    if digest.hexdigest()!=file['sha256']:
                        valid=False
                        break
                if valid:
                    return dict(row)
        return None

    def worker(batch):
        try:
            with db() as c:
                config=c.execute('SELECT * FROM apk_batches WHERE id=?',(batch,)).fetchone()
                if not config or config['status']!='queued':
                    return
                if not c.execute("UPDATE apk_batches SET status='running',message=NULL WHERE id=? AND status='queued'",(batch,)).rowcount:
                    return
            processed=0
            while not cancelled(batch):
                with db() as c:
                    row=c.execute("SELECT * FROM apk_downloads WHERE batch=? AND status='queued' ORDER BY rowid LIMIT 1",(batch,)).fetchone()
                    if not row:
                        break
                    identifier,package,arch=row['id'],row['package'],row['architecture']
                    if not c.execute("UPDATE apk_downloads SET status='downloading',error=NULL WHERE id=? AND status='queued'",(identifier,)).rowcount:
                        continue
                with db() as c:
                    c.execute('UPDATE apk_batches SET message=? WHERE id=?',(f'Checking version and local files for {package}.',batch))
                try:
                    if not auth_ready(arch):
                        raise TransportError('auth','Google Play session is missing. Refresh authentication, then resume.')
                    for attempt in range((config['max_retries'] or 0)+1):
                        try:
                            with db() as c:
                                c.execute('UPDATE apk_downloads SET attempts=attempts+1,error=NULL WHERE id=?',(identifier,))
                            stop=lambda:cancelled(batch,identifier)
                            version=available_version(package,arch,stop)
                            previous=cached(package,arch,version['version_code'])
                            if stop():
                                raise CancelledDownload('Download cancelled by user.')
                            if previous:
                                with db() as c:
                                    if not c.execute("UPDATE apk_downloads SET status='skipped',error=NULL,version_code=?,version_name=?,existing_download_id=?,finished=? WHERE id=? AND cancel_requested=0 AND status='downloading'",(previous['version_code'],previous['version_name'],previous['id'],stamp(),identifier)).rowcount:
                                        raise CancelledDownload('Download cancelled by user.')
                                break
                            stage=data/'apks'/'.staging'
                            stage.mkdir(parents=True,exist_ok=True)
                            with tempfile.TemporaryDirectory(dir=stage) as temporary:
                                with db() as c:
                                    c.execute('UPDATE apk_batches SET message=? WHERE id=?',(f'Downloading {package}; attempt {attempt+1}.',batch))
                                revision=acquire(package,arch,Path(temporary),should_cancel=stop,version=version['version_code'])
                                files=validate_set(Path(temporary),package)
                                base=next(f for f in files if f['split_name'] is None)
                                if base['version_code']!=version['version_code']:
                                    raise ValueError('Downloaded version differs from the probed version.')
                                if stop():
                                    raise CancelledDownload('Download cancelled by user.')
                                relative=Path('apks')/package/str(base['version_code'])/arch/(identifier+'-'+uuid.uuid4().hex[:8])
                                destination=data/relative
                                destination.mkdir(parents=True,exist_ok=False)
                                stored=[]
                                for index,item in enumerate(files):
                                    filename='base.apk' if item['split_name'] is None else f'split-{index:03}.apk'
                                    shutil.move(str(item['path']),destination/filename)
                                    stored.append((uuid.uuid4().hex,identifier,(relative/filename).as_posix(),'base' if item['split_name'] is None else 'split',item['split_name'],item['size_bytes'],item['sha256']))
                                with db() as c:
                                    current=c.execute('SELECT cancel_requested FROM apk_downloads WHERE id=?',(identifier,)).fetchone()[0]
                                    if current:
                                        raise CancelledDownload('Download cancelled by user.')
                                    c.executemany('INSERT INTO apk_files VALUES(?,?,?,?,?,?,?)',stored)
                                    c.execute("UPDATE apk_downloads SET status='downloaded',error=NULL,version_code=?,version_name=?,upstream_commit=?,finished=? WHERE id=?",(base['version_code'],base['version_name'],revision,stamp(),identifier))
                            break
                        except TransportError as exc:
                            if exc.kind=='transient' and attempt<(config['max_retries'] or 0):
                                delay=5*(2**attempt)+random.uniform(0,2)
                                with db() as c:
                                    c.execute('UPDATE apk_downloads SET error=? WHERE id=?',(f'Temporary error; retry {attempt+1} after {delay:.0f} seconds.',identifier))
                                wait(delay,batch,identifier)
                                continue
                            raise
                except CancelledDownload:
                    with db() as c:
                        c.execute("UPDATE apk_downloads SET status='cancelled',error='Cancelled by user.',finished=? WHERE id=? AND status!='downloaded'",(stamp(),identifier))
                except Exception as exc:
                    shared=isinstance(exc,SharedDownloadError) or isinstance(exc,TransportError) and exc.kind in ('auth','forbidden','rate_limit','transient','dependency')
                    message=str(exc) if isinstance(exc,ValueError) else f'Download/validation failed ({type(exc).__name__}).'
                    with db() as c:
                        c.execute('UPDATE apk_downloads SET status=?,error=?,finished=? WHERE id=?',('paused' if shared else 'failed',message,stamp(),identifier))
                        if shared:
                            delay=exc.retry_after if isinstance(exc,TransportError) and exc.kind=='rate_limit' else None
                            if isinstance(exc,TransportError) and exc.kind=='rate_limit' and delay is None:
                                delay=60
                            c.execute("UPDATE apk_batches SET status='paused',message=?,resume_after=?,finished=NULL WHERE id=?",(message,time.time()+delay if delay is not None else None,batch))
                    if shared:
                        return
                processed+=1
                with db() as c:
                    config=c.execute('SELECT * FROM apk_batches WHERE id=?',(batch,)).fetchone()
                with db() as c:
                    more=c.execute("SELECT 1 FROM apk_downloads WHERE batch=? AND status='queued'",(batch,)).fetchone()
                if more and not cancelled(batch):
                    delay=config['pause_seconds'] or 0
                    if processed%(config['chunk_size'] or 10)==0:
                        delay+=config['chunk_pause'] or 0
                    with db() as c:
                        c.execute('UPDATE apk_batches SET message=? WHERE id=?',(f'Waiting {delay} seconds before the next app.',batch))
                    try:
                        wait(delay,batch)
                    except CancelledDownload:
                        break
            with db() as c:
                current=c.execute('SELECT status FROM apk_batches WHERE id=?',(batch,)).fetchone()[0]
                if current=='stopping':
                    c.execute("UPDATE apk_batches SET status='stopped',message='Stopped by user; completed downloads preserved.',finished=? WHERE id=?",(stamp(),batch))
                elif current=='running':
                    failed=c.execute("SELECT COUNT(*) FROM apk_downloads WHERE batch=? AND status='failed'",(batch,)).fetchone()[0]
                    c.execute('UPDATE apk_batches SET status=?,message=NULL,finished=? WHERE id=?',('completed_with_failures' if failed else 'completed',stamp(),batch))
        except Exception:
            with db() as c:
                c.execute("UPDATE apk_downloads SET status='paused',error='Queue interrupted; resume after checking local storage.' WHERE batch=? AND status='downloading'",(batch,))
                c.execute("UPDATE apk_batches SET status='paused',message='Queue interrupted; check local storage before resuming.' WHERE id=?",(batch,))

    @api.post('/api/apks/batches')
    def create():
        body=request.get_json()
        if not isinstance(body,dict) or body.get('architecture','arm64') not in ('arm64','armv7'):
            return jsonify(error='Choose ARM64 or ARMv7.'),400
        try:
            options={key:int(body.get(key,default)) for key,default in [('pause_seconds',10),('chunk_size',10),('chunk_pause',60),('max_retries',2)]}
            if not 1<=options['pause_seconds']<=3600 or not 1<=options['chunk_size']<=1000 or not 0<=options['chunk_pause']<=3600 or not 0<=options['max_retries']<=3:
                raise ValueError()
        except (ValueError,TypeError):
            return jsonify(error='Invalid pacing settings.'),400
        arch=body.get('architecture','arm64')
        packages=body.get('packages',[])
        if not isinstance(packages,list) or any(not isinstance(p,str) or not PACKAGE.fullmatch(p) for p in packages):
            return jsonify(error='Select saved apps using valid package IDs.'),400
        with queue_lock,db() as c:
            if c.execute("SELECT 1 FROM apk_batches WHERE status IN ('queued','running','stopping')").fetchone():
                return jsonify(error='An APK batch is already running.'),409
            if body.get('all') is True:
                packages=[r[0] for r in c.execute('SELECT package FROM apps ORDER BY package')]
            packages=list(dict.fromkeys(packages))
            if not packages:
                return jsonify(error='Select at least one collected app.'),400
            for package in packages:
                if not c.execute('SELECT 1 FROM apps WHERE package=?',(package,)).fetchone():
                    return jsonify(error='Only apps already saved in the library can be downloaded.'),400
            if not auth_ready(arch):
                return jsonify(error='Google Play authentication is missing for this architecture. Run the local auth-account command first.'),409
            identifier=uuid.uuid4().hex
            c.execute('INSERT INTO apk_batches(id,status,architecture,created,pause_seconds,chunk_size,chunk_pause,max_retries) VALUES(?,?,?,?,?,?,?,?)',(identifier,'queued',arch,stamp(),options['pause_seconds'],options['chunk_size'],options['chunk_pause'],options['max_retries']))
            for package in packages:
                c.execute('INSERT INTO apk_downloads(id,batch,package,architecture,status,created) VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex,identifier,package,arch,'queued',stamp()))
        threading.Thread(target=worker,args=(identifier,),daemon=True).start()
        return jsonify(id=identifier,count=len(packages)),202

    @api.post('/api/apks/batches/<batch>/settings')
    def update_settings(batch):
        body=request.get_json()
        try:
            if not isinstance(body,dict):
                raise ValueError()
            gap=int(body['pause_seconds'])
            group=int(body['chunk_size'])
            rest=int(body['chunk_pause'])
            if not 1<=gap<=3600 or not 1<=group<=1000 or not 0<=rest<=3600:
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            return jsonify(error='Invalid pacing settings.'),400
        with db() as c:
            if not c.execute('SELECT 1 FROM apk_batches WHERE id=?',(batch,)).fetchone():
                return jsonify(error='Batch not found.'),404
            c.execute('UPDATE apk_batches SET pause_seconds=?,chunk_size=?,chunk_pause=? WHERE id=?',(gap,group,rest,batch))
        return jsonify(ok=True,message='Updated pacing applies after the current app; the transfer itself is not interrupted.')

    @api.post('/api/apks/batches/<batch>/stop')
    def stop_batch(batch):
        with queue_lock,db() as c:
            row=c.execute('SELECT status FROM apk_batches WHERE id=?',(batch,)).fetchone()
            if not row:
                return jsonify(error='Batch not found.'),404
            if row[0] not in ('queued','running','stopping','paused','blocked','interrupted'):
                return jsonify(error='This batch is already finished.'),409
            status='stopping' if row[0] in ('running','stopping') else 'stopped'
            c.execute('UPDATE apk_batches SET status=?,message=? WHERE id=?',(status,'Stopping current download and cancelling remaining apps.',batch))
            c.execute("UPDATE apk_downloads SET status='cancelled',cancelled_by='batch',finished=? WHERE batch=? AND status IN ('queued','paused','blocked')",(stamp(),batch))
            c.execute("UPDATE apk_downloads SET cancel_requested=1,cancelled_by='batch' WHERE batch=? AND status='downloading'",(batch,))
        return jsonify(ok=True)

    @api.post('/api/apks/downloads/<identifier>/cancel')
    def cancel_app(identifier):
        with db() as c:
            row=c.execute('SELECT status FROM apk_downloads WHERE id=?',(identifier,)).fetchone()
            if not row:
                return jsonify(error='Download not found.'),404
            if row[0]=='downloading':
                c.execute("UPDATE apk_downloads SET cancel_requested=1,cancelled_by='app' WHERE id=?",(identifier,))
            elif row[0] in ('queued','paused','blocked'):
                c.execute("UPDATE apk_downloads SET status='cancelled',cancelled_by='app',finished=? WHERE id=?",(stamp(),identifier))
            else:
                return jsonify(error='Only unfinished downloads can be cancelled.'),409
        return jsonify(ok=True)

    @api.post('/api/apks/batches/<batch>/resume')
    def resume(batch):
        with queue_lock,db() as c:
            if c.execute("SELECT 1 FROM apk_batches WHERE status IN ('queued','running','stopping')").fetchone():
                return jsonify(error='Another batch is still active.'),409
            row=c.execute('SELECT * FROM apk_batches WHERE id=?',(batch,)).fetchone()
            if not row:
                return jsonify(error='Batch not found.'),404
            if row['resume_after'] and row['resume_after']>time.time():
                response=jsonify(error='Rate-limit waiting period has not elapsed.',resume_after=row['resume_after'])
                response.status_code=429
                response.headers['Retry-After']=str(max(1,int(row['resume_after']-time.time())))
                return response
            if not auth_ready(row['architecture']):
                return jsonify(error='Refresh the saved Play session first.'),409
            c.execute("UPDATE apk_downloads SET status='queued',cancel_requested=0,error=NULL,finished=NULL WHERE batch=? AND (status IN ('queued','paused','blocked','failed') OR (status='cancelled' AND cancelled_by='batch'))",(batch,))
            count=c.execute("SELECT COUNT(*) FROM apk_downloads WHERE batch=? AND status='queued'",(batch,)).fetchone()[0]
            if not count:
                return jsonify(error='No unfinished apps to resume. Individually cancelled apps stay cancelled.'),409
            c.execute("UPDATE apk_batches SET status='queued',message=NULL,finished=NULL,resume_after=NULL WHERE id=?",(batch,))
        threading.Thread(target=worker,args=(batch,),daemon=True).start()
        return jsonify(ok=True,count=count)

    @api.get('/api/apks')
    def state():
        package=request.args.get('package')
        with db() as c:
            c.execute('BEGIN')
            sql='SELECT d.*,a.metadata FROM apk_downloads d LEFT JOIN apps a ON a.package=d.package'
            rows=c.execute(sql+(' WHERE d.package=?' if package else '')+' ORDER BY d.created DESC',(package,) if package else ()).fetchall()
            downloads=[]
            for row in rows:
                item=dict(row)
                item['title']=json.loads(item.pop('metadata') or '{}').get('title',item['package'])
                item['files']=[dict(f) for f in c.execute('SELECT * FROM apk_files WHERE download_id=?',(item.get('existing_download_id') or row['id'],))]
                downloads.append(item)
            batches=[dict(r) for r in c.execute('SELECT * FROM apk_batches ORDER BY created DESC LIMIT 20')]
            counts={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM apk_downloads GROUP BY status')}
            file_totals=c.execute("SELECT COUNT(*),COALESCE(SUM(f.size_bytes),0),COALESCE(SUM(CASE WHEN f.kind='base' THEN 1 ELSE 0 END),0),COALESCE(SUM(CASE WHEN f.kind='split' THEN 1 ELSE 0 END),0) FROM apk_files f JOIN apk_downloads d ON d.id=f.download_id WHERE d.status='downloaded'").fetchone()
            verified="status='downloaded' AND EXISTS(SELECT 1 FROM apk_files f WHERE f.download_id=d.id AND f.kind='base')"
            stats=dict(total_apps=c.execute('SELECT COUNT(*) FROM apps').fetchone()[0],
                downloaded_apps=c.execute(f'SELECT COUNT(DISTINCT package) FROM apk_downloads d WHERE {verified}').fetchone()[0],
                version_sets=c.execute(f'SELECT COUNT(*) FROM (SELECT package,version_code,architecture FROM apk_downloads d WHERE {verified} GROUP BY package,version_code,architecture)').fetchone()[0],
                total_files=file_totals[0],total_bytes=file_totals[1],base_files=file_totals[2],split_files=file_totals[3],
                successful_attempts=counts.get('downloaded',0),reused_attempts=counts.get('skipped',0),failed_attempts=counts.get('failed',0),
                queued_attempts=counts.get('queued',0),active_attempts=counts.get('downloading',0),paused_attempts=counts.get('paused',0)+counts.get('blocked',0),cancelled_attempts=counts.get('cancelled',0),
                free_disk_bytes=shutil.disk_usage(data).free,scope='All batches; file totals reflect registered successful downloads, not a fresh disk integrity scan.')
            stats['not_downloaded_apps']=max(0,stats['total_apps']-stats['downloaded_apps'])

        return jsonify(downloads=downloads,batches=batches,stats=stats,auth=dict(arm64=auth_ready('arm64'),armv7=auth_ready('armv7')))

    @api.get('/api/apks/files/<identifier>')
    def file(identifier):
        with db() as c:
            row=c.execute("SELECT f.* FROM apk_files f JOIN apk_downloads d ON d.id=f.download_id WHERE f.id=? AND d.status='downloaded'",(identifier,)).fetchone()
        if not row:
            return jsonify(error='APK file not found'),404
        path=(data/row['relative_path']).resolve()
        if not path.is_relative_to(data/'apks') or not path.is_file():
            return jsonify(error='APK file is missing from local storage.'),404
        return send_file(path,mimetype='application/vnd.android.package-archive',as_attachment=True,download_name=path.name)

    app.register_blueprint(api)
    return worker
