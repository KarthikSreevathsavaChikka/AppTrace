"""Download original APKs through the pinned upstream CLI and inventory them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+")
    parser.add_argument("--arch", choices=("arm64", "armv7"), default="arm64")
    args = parser.parse_args()
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", p) for p in args.packages):
        parser.error("Provide Android package IDs, not URLs")
    pin = json.loads((ROOT / "upstream.json").read_text())
    checkout = ROOT / pin["checkout"]
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if revision != pin["commit"]:
        parser.error("Upstream checkout differs from pinned commit")
    env = dict(os.environ, PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION="python")
    run = ROOT / "artifacts" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run.mkdir(parents=True, mode=0o700)
    report = {"upstream_commit": revision, "architecture": args.arch, "apps": []}
    for package in args.packages:
        output = run / package
        output.mkdir()
        print(f"Validating {package}", flush=True)
        # Inherit the terminal: do not persist potentially sensitive upstream logs.
        try:
            result = subprocess.run([
                sys.executable, str(checkout / "gplay-downloader.py"),
                "download", package, "--arch", args.arch, "--output", str(output),
            ], env=env, timeout=1800)
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
        app = {"package_id": package, "play_url": f"https://play.google.com/store/apps/details?id={package}",
               "collected_at": datetime.now(timezone.utc).isoformat(), "exit_code": exit_code, "files": []}
        for path in sorted(output.glob("*.apk")):
            valid = False
            try:
                with zipfile.ZipFile(path) as archive:
                    valid = "AndroidManifest.xml" in archive.namelist() and archive.testzip() is None
            except (OSError, zipfile.BadZipFile, RuntimeError):
                pass
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            app["files"].append({"path": str(path.relative_to(ROOT)), "size_bytes": path.stat().st_size,
                                 "sha256": digest.hexdigest(), "valid_apk_container": valid})
        app["status"] = "passed" if exit_code == 0 and app["files"] and all(f["valid_apk_container"] for f in app["files"]) else "failed"
        report["apps"].append(app)
        (run / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {run / 'report.json'}")
    print("Container integrity checked; signatures and delivery completeness are not verified.")
    return int(any(app["status"] != "passed" for app in report["apps"]))


if __name__ == "__main__":
    raise SystemExit(main())
