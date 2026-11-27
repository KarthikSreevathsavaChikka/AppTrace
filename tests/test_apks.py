import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from flask import Flask
from collector import apks


class ApkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.data=Path(self.temp.name)
        def db():
            c=sqlite3.connect(self.data/'collector.sqlite')
            c.row_factory=sqlite3.Row
            return c
        self.db=db
        with db() as c:
            c.execute('CREATE TABLE apps(package TEXT PRIMARY KEY,metadata TEXT)')
            c.execute('INSERT INTO apps VALUES(?,?)',('com.example.app',json.dumps({'title':'Example'})))
        self.app=Flask(__name__)
        self.worker=apks.install(self.app,db,self.data)
        self.client=self.app.test_client()
        probe=patch.object(apks,'available_version',return_value={'version_code':123,'version_name':'1.2.3'})
        probe.start()
        self.addCleanup(probe.stop)

    def tearDown(self):
        self.temp.cleanup()

    def queue(self):
        with self.db() as c:
            c.execute("INSERT INTO apk_batches(id,status,architecture,created,finished) VALUES('batch','queued','arm64',?,NULL)",(apks.stamp(),))
            c.execute("INSERT INTO apk_downloads(id,batch,package,architecture,status,created) VALUES('download','batch','com.example.app','arm64','queued',?)",(apks.stamp(),))

    @staticmethod
    def acquire(package,arch,directory,**kwargs):
        (directory/'base.apk').write_bytes(b'original base bytes')
        (directory/'split.apk').write_bytes(b'original split bytes')
        return 'pinned-revision'

    @staticmethod
    def manifest(path):
        return dict(package='com.example.app',version_code=123,version_name='1.2.3',split_name=None if path.name=='base.apk' else 'config.en')

    def test_download_links_files_to_manifest_verified_app(self):
        self.queue()
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'acquire',side_effect=self.acquire),patch.object(apks,'inspect_manifest',side_effect=self.manifest):
            self.worker('batch')
        result=self.client.get('/api/apks?package=com.example.app').get_json()
        download=result['downloads'][0]
        self.assertEqual(download['status'],'downloaded')
        self.assertEqual(download['package'],'com.example.app')
        self.assertEqual(download['version_code'],123)
        self.assertEqual(len(download['files']),2)
        base=next(f for f in download['files'] if f['kind']=='base')
        self.assertTrue(base['relative_path'].startswith('apks/com.example.app/123/arm64/'))
        self.assertEqual((self.data/base['relative_path']).read_bytes(),b'original base bytes')
        response=self.client.get('/api/apks/files/'+base['id'])
        self.assertEqual(response.data,b'original base bytes')
        response.close()
        with self.db() as c:
            c.execute('UPDATE apk_files SET relative_path=? WHERE id=?',('../outside.apk',base['id']))
        self.assertEqual(self.client.get('/api/apks/files/'+base['id']).status_code,404)

    def test_package_mismatch_fails_without_registering_files(self):
        self.queue()
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'acquire',side_effect=self.acquire),patch.object(apks,'inspect_manifest',return_value=dict(package='com.other.app',version_code=123,version_name='1',split_name=None)):
            self.worker('batch')
        download=self.client.get('/api/apks').get_json()['downloads'][0]
        self.assertEqual(download['status'],'failed')
        self.assertEqual(download['files'],[])
        self.assertIn('does not match',download['error'])

    def test_queue_validation_auth_and_deduplication(self):
        with patch.object(apks,'auth_ready',return_value=False):
            self.assertEqual(self.client.post('/api/apks/batches',json={'all':True}).status_code,409)
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks.threading,'Thread'):
            self.assertEqual(self.client.post('/api/apks/batches',json={'packages':['com.unknown.app']}).status_code,400)
            self.assertEqual(self.client.post('/api/apks/batches',json={'architecture':'x86','all':True}).status_code,400)
            response=self.client.post('/api/apks/batches',json={'packages':['com.example.app','com.example.app']})
            self.assertEqual(response.status_code,202)
            self.assertEqual(response.get_json()['count'],1)
            self.assertEqual(self.client.post('/api/apks/batches',json={'all':True}).status_code,409)

    def test_expired_session_pauses_batch_and_preserves_remaining_apps(self):
        self.queue()
        with self.db() as c:
            c.execute("INSERT INTO apps VALUES('com.example.second','{}')")
            c.execute("INSERT INTO apk_downloads(id,batch,package,architecture,status,created) VALUES('second','batch','com.example.second','arm64','queued',?)",(apks.stamp(),))
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'acquire',side_effect=apks.download_error('Failed to get app details: 401')) as acquire:
            self.worker('batch')
        self.assertEqual(acquire.call_count,1)
        result=self.client.get('/api/apks').get_json()
        self.assertEqual(result['batches'][0]['status'],'paused')
        self.assertEqual({r['status'] for r in result['downloads']},{'paused','queued'})
        self.assertTrue(any('401' in (r['error'] or '') for r in result['downloads']))

    def test_safe_error_classification(self):
        self.assertIsInstance(apks.download_error('NameResolutionError: Failed to resolve host'),apks.SharedDownloadError)
        self.assertIsInstance(apks.download_error('ModuleNotFoundError: gpapi'),apks.SharedDownloadError)
        self.assertNotIsInstance(apks.download_error('Failed to get app details: 404'),apks.SharedDownloadError)
        private='secret-token-must-not-appear'
        self.assertNotIn(private,str(apks.download_error('Failed to get app details: 401 '+private)))

    def test_cancel_queued_app_and_stop_resume_batch_preserve_files(self):
        self.queue()
        self.assertEqual(self.client.post('/api/apks/downloads/download/cancel',json={}).status_code,200)
        self.worker('batch')
        result=self.client.get('/api/apks').get_json()
        self.assertEqual(result['downloads'][0]['status'],'cancelled')
        self.assertEqual(self.client.post('/api/apks/batches/batch/resume',json={}).status_code,409)

    def test_stop_queued_batch_can_resume_but_individual_cancellations_stay(self):
        self.queue()
        self.assertEqual(self.client.post('/api/apks/batches/batch/stop',json={}).status_code,200)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT status FROM apk_downloads WHERE id='download'").fetchone()[0],'cancelled')
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks.threading,'Thread'):
            self.assertEqual(self.client.post('/api/apks/batches/batch/resume',json={}).status_code,200)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT status FROM apk_downloads WHERE id='download'").fetchone()[0],'queued')

    def test_rate_limit_pauses_and_enforces_retry_after(self):
        self.queue()
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'available_version',side_effect=apks.TransportError('rate_limit','HTTP 429',120)):
            self.worker('batch')
        with self.db() as c:
            row=c.execute("SELECT * FROM apk_batches WHERE id='batch'").fetchone()
            self.assertEqual(row['status'],'paused')
            self.assertGreater(row['resume_after'],apks.time.time()+110)
        response=self.client.post('/api/apks/batches/batch/resume',json={})
        self.assertEqual(response.status_code,429)
        self.assertGreater(int(response.headers['Retry-After']),100)

    def test_transient_retries_are_bounded_and_forbidden_pauses(self):
        self.queue()
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'available_version',side_effect=apks.TransportError('transient','HTTP 503')) as probe,patch.object(apks,'paced_wait') as wait:
            self.worker('batch')
        self.assertEqual(probe.call_count,3)
        self.assertEqual(wait.call_count,2)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT status FROM apk_batches WHERE id='batch'").fetchone()[0],'paused')
            c.execute("UPDATE apk_batches SET status='queued' WHERE id='batch'")
            c.execute("UPDATE apk_downloads SET status='queued' WHERE batch='batch'")
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'available_version',side_effect=apks.TransportError('forbidden','HTTP 403')) as probe:
            self.worker('batch')
        self.assertEqual(probe.call_count,1)
        self.assertEqual(self.client.get('/api/apks').get_json()['batches'][0]['status'],'paused')

    def test_verified_same_version_is_reused_without_download(self):
        self.queue()
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'acquire',side_effect=self.acquire),patch.object(apks,'inspect_manifest',side_effect=self.manifest):
            self.worker('batch')
        with self.db() as c:
            c.execute("INSERT INTO apk_batches(id,status,architecture,created) VALUES('again','queued','arm64',?)",(apks.stamp(),))
            c.execute("INSERT INTO apk_downloads(id,batch,package,architecture,status,created) VALUES('again','again','com.example.app','arm64','queued',?)",(apks.stamp(),))
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'acquire') as acquire:
            self.worker('again')
        acquire.assert_not_called()
        result=self.client.get('/api/apks').get_json()
        reused=next(r for r in result['downloads'] if r['id']=='again')
        self.assertEqual(reused['status'],'skipped')
        self.assertEqual(len(reused['files']),2)
        self.assertEqual(reused['existing_download_id'],'download')
        self.assertEqual(result['stats']['downloaded_apps'],1)
        self.assertEqual(result['stats']['version_sets'],1)
        self.assertEqual(result['stats']['successful_attempts'],1)
        self.assertEqual(result['stats']['reused_attempts'],1)
        self.assertEqual(result['stats']['total_files'],2)

    def test_restart_preserves_success_and_pauses_unfinished(self):
        self.queue()
        with self.db() as c:
            c.execute("UPDATE apk_batches SET status='running' WHERE id='batch'")
            c.execute("UPDATE apk_downloads SET status='downloading' WHERE id='download'")
        other=Flask('restart')
        apks.install(other,self.db,self.data)
        result=other.test_client().get('/api/apks').get_json()
        self.assertEqual(result['batches'][0]['status'],'paused')
        self.assertEqual(result['downloads'][0]['status'],'paused')

    def test_cancel_terminates_download_subprocess(self):
        import subprocess
        original=subprocess.Popen
        children=[]
        def launch(command,**kwargs):
            child=original([apks.sys.executable,'-c','import time; time.sleep(30)'],**kwargs)
            children.append(child)
            return child
        with patch.object(apks,'upstream',return_value=(self.data,'revision')),patch.object(apks.subprocess,'Popen',side_effect=launch):
            with self.assertRaises(apks.CancelledDownload):
                apks.transport('download','com.example.app','arm64',self.data,should_cancel=lambda:True)
        self.assertIsNotNone(children[0].poll())

    def test_cancel_active_app_continues_remaining_queue(self):
        self.queue()
        with self.db() as c:
            c.execute("INSERT INTO apps VALUES('com.example.second','{}')")
            c.execute("INSERT INTO apk_downloads(id,batch,package,architecture,status,created) VALUES('second','batch','com.example.second','arm64','queued',?)",(apks.stamp(),))
        def acquire(package,arch,directory,**kwargs):
            if package=='com.example.app':
                self.client.post('/api/apks/downloads/download/cancel',json={})
                self.assertTrue(kwargs['should_cancel']())
                raise apks.CancelledDownload('Cancelled')
            return self.acquire(package,arch,directory)
        def manifest(path):
            return dict(self.manifest(path),package='com.example.second')
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'acquire',side_effect=acquire),patch.object(apks,'inspect_manifest',side_effect=manifest),patch.object(apks,'paced_wait'):
            self.worker('batch')
        result=self.client.get('/api/apks').get_json()
        statuses={row['id']:row['status'] for row in result['downloads']}
        self.assertEqual(statuses,{'download':'cancelled','second':'downloaded'})

    def test_stop_active_batch_cancels_remaining_without_files(self):
        self.queue()
        with self.db() as c:
            c.execute("INSERT INTO apk_downloads(id,batch,package,architecture,status,created) VALUES('second','batch','com.example.app','arm64','queued',?)",(apks.stamp(),))
        def acquire(package,arch,directory,**kwargs):
            self.client.post('/api/apks/batches/batch/stop',json={})
            self.assertTrue(kwargs['should_cancel']())
            raise apks.CancelledDownload('Cancelled')
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'acquire',side_effect=acquire) as fetch:
            self.worker('batch')
        result=self.client.get('/api/apks').get_json()
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(result['batches'][0]['status'],'stopped')
        self.assertTrue(all(row['status']=='cancelled' and not row['files'] for row in result['downloads']))

    def test_live_batch_pacing_can_be_updated_without_cancelling(self):
        self.queue()
        response=self.client.post('/api/apks/batches/batch/settings',json={'pause_seconds':2,'chunk_size':20,'chunk_pause':15})
        self.assertEqual(response.status_code,200)
        with self.db() as c:
            row=c.execute("SELECT * FROM apk_batches WHERE id='batch'").fetchone()
            self.assertEqual((row['pause_seconds'],row['chunk_size'],row['chunk_pause']),(2,20,15))
            self.assertEqual(c.execute("SELECT status FROM apk_downloads WHERE id='download'").fetchone()[0],'queued')
        self.assertEqual(self.client.post('/api/apks/batches/batch/settings',json={'pause_seconds':-1,'chunk_size':1,'chunk_pause':1}).status_code,400)

    def test_global_stats_separate_files_apps_and_failed_history(self):
        self.queue()
        with patch.object(apks,'auth_ready',return_value=True),patch.object(apks,'acquire',side_effect=self.acquire),patch.object(apks,'inspect_manifest',side_effect=self.manifest):
            self.worker('batch')
        with self.db() as c:
            c.execute("INSERT INTO apps VALUES('com.example.other','{}')")
            c.execute("INSERT INTO apk_downloads(id,batch,package,architecture,status,created) VALUES('failed','batch','com.example.app','arm64','failed',?)",(apks.stamp(),))
        stats=self.client.get('/api/apks').get_json()['stats']
        self.assertEqual(stats['total_apps'],2)
        self.assertEqual(stats['downloaded_apps'],1)
        self.assertEqual(stats['not_downloaded_apps'],1)
        self.assertEqual(stats['base_files'],1)
        self.assertEqual(stats['split_files'],1)
        self.assertEqual(stats['failed_attempts'],1)
        self.assertEqual(stats['successful_attempts'],1)
        self.assertEqual(stats['total_bytes'],len(b'original base bytes')+len(b'original split bytes'))
        self.assertGreater(stats['free_disk_bytes'],0)

    def test_retry_after_formats(self):
        from collector.http_limits import retry_after_seconds
        from email.utils import format_datetime
        from datetime import datetime,timezone,timedelta
        self.assertEqual(retry_after_seconds('120'),120)
        self.assertGreater(retry_after_seconds(format_datetime(datetime.now(timezone.utc)+timedelta(minutes=2))),110)
        self.assertIsNone(retry_after_seconds('not a date'))
        self.assertIsNone(retry_after_seconds('inf'))

    def test_split_versions_must_match_and_base_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            self.acquire('com.example.app','arm64',path)
            def mismatch(file):
                return dict(self.manifest(file),version_code=1 if file.name=='base.apk' else 2)
            with patch.object(apks,'inspect_manifest',side_effect=mismatch):
                with self.assertRaisesRegex(ValueError,'versions do not match'):
                    apks.validate_set(path,'com.example.app')
            with patch.object(apks,'inspect_manifest',return_value=dict(package='com.example.app',version_code=1,split_name='config.en')):
                with self.assertRaisesRegex(ValueError,'exactly one base'):
                    apks.validate_set(path,'com.example.app')
