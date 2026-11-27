"""Run the pinned downloader with structured, credential-free HTTP diagnostics."""
import argparse
import contextlib
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION']='python'
root=Path(__file__).resolve().parents[1]
checkout=root/'vendor/gplay-apk-downloader'
sys.path.insert(0,str(root))
sys.path.insert(0,str(checkout))
from collector.http_limits import retry_after_seconds
from collector.transfer import buffered_response,QuietProgress
p=argparse.ArgumentParser()
p.add_argument('mode',choices=['probe','download'])
p.add_argument('package')
p.add_argument('--arch',choices=['arm64','armv7'],default='arm64')
p.add_argument('--output')
p.add_argument('--version',type=int)
args=p.parse_args()


def fail(kind,message,retry_after=None):
    print('APPTRACE_ERROR '+json.dumps(dict(kind=kind,message=message,retry_after=retry_after)),file=sys.stderr,flush=True)


try:
    import requests
    from gpapi import googleplay_pb2
    original=requests.sessions.Session.request
    def checked(session,*a,**kw):
        try:
            response=original(session,*a,**kw)
        except requests.RequestException:
            fail('transient','Network or TLS request failed.')
            raise
        status=response.status_code
        if status in (401,403,429) or 500<=status<=599:
            delay=retry_after_seconds(response.headers.get('Retry-After'))
            kind={401:'auth',403:'forbidden',429:'rate_limit'}.get(status,'transient')
            response.close()
            fail(kind,f'Google Play returned HTTP {status}.',delay)
            raise RuntimeError(f'HTTP {status}')
        if kw.get('stream'):
            buffered_response(response,requests.RequestException,fail)
        return response
    requests.sessions.Session.request=checked
    pool=requests.Session()
    requests.get=pool.get
    requests.post=pool.post
    spec=importlib.util.spec_from_file_location('apptrace_upstream',checkout/'gplay-downloader.py')
    upstream=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upstream)
    if args.mode=='probe':
        arch='armeabi-v7a' if args.arch=='armv7' else 'arm64-v8a'
        auth=upstream.load_auth(arch)
        if not auth:
            fail('auth','No saved session for the requested architecture.')
            raise SystemExit(1)
        response=requests.get(upstream.DETAILS_URL,params={'doc':args.package},headers=upstream.get_auth_headers(auth),timeout=30)
        if response.status_code!=200:
            fail('app',f'App details returned HTTP {response.status_code}.')
            raise SystemExit(1)
        wrapper=googleplay_pb2.ResponseWrapper()
        wrapper.ParseFromString(response.content)
        doc=wrapper.payload.detailsResponse.docV2
        version=doc.details.appDetails.versionCode
        if not doc.docid or not version:
            fail('app','No compatible app version is available for this account/device.')
            raise SystemExit(1)
        print(json.dumps(dict(version_code=version,version_name=doc.details.appDetails.versionString)))
    else:
        from types import SimpleNamespace
        progress=QuietProgress()
        with contextlib.redirect_stdout(progress):
            result=upstream.cmd_download(SimpleNamespace(package=args.package,arch=args.arch,output=args.output,version=args.version,merge=False,install=False))
        if result:
            print(progress.details())
        raise SystemExit(result or 0)
except SystemExit:
    raise
except ImportError:
    fail('dependency','Downloader dependencies are missing from the server Python environment.')
    raise SystemExit(1)
except Exception:
    # The HTTP hook already emits a structured error; do not expose raw credentials.
    raise SystemExit(1)

finally:
    if 'pool' in globals():
        pool.close()
