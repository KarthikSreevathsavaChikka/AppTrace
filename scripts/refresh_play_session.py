"""Refresh a Play session from its saved AAS token without exposing credentials."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION']='python'
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'vendor/gplay-apk-downloader'))
import account_auth
from device_profiles import NEWEST_ARMV7_PROFILE

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--arch',choices=['arm64','armv7','both'],default='both')
args=parser.parse_args()
failed=False
for arch in (['arm64','armv7'] if args.arch=='both' else [args.arch]):
    path=Path.home()/('.gplay-auth-armv7.json' if arch=='armv7' else '.gplay-auth.json')
    try:
        saved=json.loads(path.read_text())
        if not saved.get('email') or not saved.get('aasToken'):
            print(f'{arch}: no saved account/AAS token; use auth-account --browser locally.')
            failed=True
            continue
        fresh=account_auth.build_auth_data(saved['email'],saved['aasToken'],device=NEWEST_ARMV7_PROFILE if arch=='armv7' else None)
        with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.gplay-refresh-',delete=False) as stream:
            temporary=Path(stream.name)
            os.chmod(temporary,0o600)
            json.dump(fresh,stream,indent=2)
        os.replace(temporary,path)
        print(f'{arch}: session refreshed; credentials kept in the local auth file.')
    except Exception as exc:
        print(f'{arch}: refresh failed ({type(exc).__name__}); use auth-account --browser locally.')
        failed=True
raise SystemExit(int(failed))
