#!/usr/bin/env bash
# The Windows server binary's smoke test (CI, Git Bash): the same steps on
# GitHub's x64 runner and on windows-11-arm, where the x64 build runs under
# Windows 11's x64 emulation (David, 16 Oct 2026). No network beyond
# loopback; nothing published (MEDIA_LOOKUP_URL=- / lookup.invalid).
#
#   deploy/binary/smoke-windows.sh <path to sasonica-windows-x86_64.exe>
set -euxo pipefail
b=$1
# The runner's own profile: PyApp finds its folders through Windows
# (known folders), which a made-up USERPROFILE breaks.
"$b" version
"$b" commands | tr -d '\r' | grep -qx media
"$b" serve --bind 127.0.0.1 --port 18781 >serve.log 2>&1 &
spid=$!
"$b" sessiond >sessiond.log 2>&1 &
dpid=$!
for i in $(seq 1 90); do curl -fsS http://127.0.0.1:18781/healthz >/dev/null && break; sleep 1; done
curl -fsS http://127.0.0.1:18781/healthz
# sessiond over loopback TCP: its address file, and it answers.
for i in $(seq 1 30); do grep -q "listening on" sessiond.log && break; sleep 1; done
cat sessiond.log
grep -q "listening on 127.0.0.1:" sessiond.log
"$b" install --dry-run --no-services | tee install.txt
kill $spid $dpid || true
# The quick tunnel is Windows' too: install fetches Cloudflare's pinned
# Windows build, x64 on ARM64 as well (or finds one already there).
tr -d '\r' <install.txt | grep -Eq 'cloudflared: (would fetch .*/cloudflared-windows-amd64\.exe|.*(already there|on PATH))'
# A tunnel's URL into `pair --device`, through a stand-in cloudflared (no
# network): media-tunnel run reads its name off stderr, and the pairing link
# names it and its lookup.
printf '@echo INF ^|  https://ci-fake-tunnel.trycloudflare.com  ^| 1>&2\r\n@ping -n 60 127.0.0.1 >nul\r\n' >fakecf.cmd
MEDIA_CLOUDFLARED="$(cygpath -w "$PWD/fakecf.cmd")" MEDIA_LOOKUP_URL=- \
  "$b" media-tunnel run >tunnel.log 2>&1 &
tpid=$!
for i in $(seq 1 60); do "$b" media-tunnel url >/dev/null 2>&1 && break; sleep 1; done
cat tunnel.log
"$b" media-tunnel url | tr -d '\r' | grep -qx 'https://ci-fake-tunnel.trycloudflare.com'
# MEDIA_LOOKUP_URL=-: the stand-in's URL is not published to sasonica.com/r.
for i in $(seq 1 20); do grep -q 'media-tunnel: lookup ' tunnel.log && break; sleep 1; done
grep -q 'media-tunnel: lookup failed: lookup off' tunnel.log
! grep -q 'lookup updated' tunnel.log
MEDIA_LOOKUP_URL=https://lookup.invalid/r "$b" pair --device CI | tr -d '\r' | tee pair.txt
grep -q 'sasonica://pair?server=https%3A%2F%2Fci-fake-tunnel.trycloudflare.com&code=' pair.txt
grep -q '&lookup=https%3A%2F%2Flookup.invalid%2Fr%2F[a-z2-7]\{26\}' pair.txt
kill $tpid || true
# The audio seam's windows backend, from inside the .exe: chosen, and its
# winrt projection (a win32-only dependency) importable.
"$b" media-audio --json | tee audio.json
grep -q '"backend": "windows"' audio.json
! grep -q '"winrt": "not importable' audio.json
# The opt-in speech player's launcher: its pipe and mpv command line.
"$b" media-sink-speech --print | tee speech.txt
grep -q 'pipe' speech.txt
"$b" install --dry-run --no-services --local-speech
