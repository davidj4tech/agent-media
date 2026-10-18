"""`GET /.well-known/assetlinks.json`: Android App Links for the Sasonica app.

A thread link is `https://<server>/app/t/<session>` (sasonica-app
lib/threadLinks.ts; David, 18 Oct 2026): in a browser it is the web app's
thread (web_app.py), and on a phone with the app Android hands it to the app
instead, once this file on the host vouches for it. The app's manifest
claims the host with `autoVerify`; Android fetches this file over https and
checks that it names the app's package and the key it is signed with.

The packages and keys are the app's own, the same on every server: the
release build (the Sasonica release key, the one the phone runs) and the
debug build (`com.sasonica.app.debug`, android/app/sasonica-debug.keystore).
MEDIA_APP_LINKS_EXTRA adds fingerprints for a build signed some other way,
`<package>=<sha256>` pairs separated by spaces.

Open, with no credential, on every listener, the public one too (contract
§19): Android asks without one, and it says only what any APK says itself.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler

PATH = "/.well-known/assetlinks.json"

# keytool -printcert -jarfile <apk> (release, CI build-1285) and
# keytool -list -v -keystore android/app/sasonica-debug.keystore (debug).
APPS: dict[str, list[str]] = {
    "com.sasonica.app": [
        "F2:AF:5D:6E:4D:A8:BB:47:01:EA:A1:8C:C8:69:53:E2:B1:71:B8:A8:4A:4D:CC:BD:C1:8C:32:BA:4C:AD:34:64",
    ],
    "com.sasonica.app.debug": [
        "BC:7F:F5:3C:A7:D6:30:CE:D7:F7:8C:34:FA:E0:76:10:3F:E4:44:7B:F4:83:68:44:EB:94:50:D6:2C:F0:25:65",
    ],
}


def statements() -> list[dict]:
    apps = {k: list(v) for k, v in APPS.items()}
    for pair in os.environ.get("MEDIA_APP_LINKS_EXTRA", "").split():
        pkg, _, sha = pair.partition("=")
        if pkg and sha and sha not in apps.setdefault(pkg, []):
            apps[pkg].append(sha.upper())
    return [{"relation": ["delegate_permission/common.handle_all_urls"],
             "target": {"namespace": "android_app", "package_name": pkg,
                        "sha256_cert_fingerprints": shas}}
            for pkg, shas in apps.items()]


def serve(h: BaseHTTPRequestHandler) -> bool:
    body = json.dumps(statements(), indent=2).encode() + b"\n"
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.send_header("Cache-Control", "public, max-age=3600")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)
    return True
