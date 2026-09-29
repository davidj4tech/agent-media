#!/usr/bin/env bash
# Build Sasonica's server as one file: build/binary/sasonica-<os>-<arch>
# (linux or macos; x86_64 or aarch64).
#
#   deploy/binary/build.sh            # this machine's architecture
#
# docs/proposals/2026-09-29-single-binary.md. The file is a PyApp launcher
# (a small Rust program) with a whole Python inside it — python-build-standalone,
# with core, server, visual and the `sasonica` entry package already installed.
# Its first run unpacks that into ~/.local/share/pyapp/sasonica/ (a second or
# two); every run after checks the directory is there and execs Python, so a
# hook called every turn pays no unpacking. Nothing is fetched at run time.
#
# Native only: the Python packages are installed by the Python they will run
# under, so an arm64 binary is built on arm64 (CI: ubuntu-24.04-arm).
#
# On Linux the launcher is compiled in Debian bullseye (rust:1-bullseye), so
# it needs glibc 2.31 or newer: Debian 11+, Ubuntu 20.04+, the phone's Debian
# proot. With cargo on PATH it is compiled here instead (CI runs this inside
# that image); without, in podman or docker. On a Mac, with its own cargo.
#
# Settings (environment):
#   SASONICA_VERSION   the version (default: <UTC date>+<commit>); PyApp keeps
#                      each version in its own directory, so it must change
#                      whenever the contents do
#   PBS_TAG, PY_VERSION   python-build-standalone release and CPython version
#   PYAPP_VERSION      the PyApp release the launcher is built from
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PBS_TAG=${PBS_TAG:-20260924}
PY_VERSION=${PY_VERSION:-3.12.14}
PYAPP_VERSION=${PYAPP_VERSION:-v0.29.0}
ARCH=$(uname -m)
case $ARCH in
  x86_64 | aarch64) ;;
  arm64) ARCH=aarch64 ;;
  *) echo "build: no python-build-standalone for $ARCH" >&2; exit 1 ;;
esac
case $(uname -s) in
  Linux) OS=linux TRIPLE=$ARCH-unknown-linux-gnu ;;
  Darwin) OS=macos TRIPLE=$ARCH-apple-darwin ;;
  *) echo "build: not built on $(uname -s)" >&2; exit 1 ;;
esac
VERSION=${SASONICA_VERSION:-$(date -u +%Y.%m.%d)+$(git -C "$ROOT" rev-parse --short HEAD)}
PY_MINOR=${PY_VERSION%.*}
WORK=$ROOT/build/binary/$ARCH
OUT=$ROOT/build/binary/sasonica-$OS-$ARCH

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

rm -rf "$WORK" && mkdir -p "$WORK/dist"

step "Python $PY_VERSION ($OS $ARCH)"
pbs=cpython-$PY_VERSION+$PBS_TAG-$TRIPLE-install_only_stripped.tar.gz
curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/$pbs" |
  tar -xz -C "$WORK/dist"
PY=$WORK/dist/python/bin/python3

step "Packages"
"$PY" -m pip install -q --no-cache-dir --no-warn-script-location \
  "$ROOT/packages/core" "$ROOT/packages/server" "$ROOT/packages/visual" "$ROOT/packages/sasonica"
SITE=$WORK/dist/python/lib/python$PY_MINOR/site-packages
echo "$VERSION" >"$SITE/sasonica/BUILD"
# pip wrote each console script with this build directory in its #! line.
# Make them find the Python beside them instead, wherever it is unpacked:
# pip's own trick for a #! too long for the kernel, a shell line Python reads
# as a string.
for f in "$WORK"/dist/python/bin/*; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  head -c 2 "$f" | grep -q '#!' || continue
  first=$(head -n 1 "$f")
  case $first in
    "#!$WORK"/*)
      { printf '#!/bin/sh\n'
        printf "'''exec' \"\$(dirname -- \"\$(realpath -- \"\$0\")\")/python3\" \"\$0\" \"\$@\"\n"
        printf "' '''\n"
        tail -n +2 "$f"; } >"$f.new"
      chmod 755 "$f.new" && mv "$f.new" "$f" ;;
  esac
done
"$PY" -m compileall -q -j 0 "$SITE" >/dev/null || true
tar -C "$WORK/dist" -czf "$WORK/python.tar.gz" python
echo "$(du -h "$WORK/python.tar.gz" | cut -f1) packed"

step "Launcher (PyApp $PYAPP_VERSION)"
mkdir -p "$WORK/pyapp"
curl -fsSL "https://github.com/ofek/pyapp/releases/download/$PYAPP_VERSION/source.tar.gz" |
  tar -xz -C "$WORK/pyapp" --strip-components=1
# What the launcher is told at compile time (PyApp reads these in build.rs).
cat >"$WORK/pyapp.env" <<EOF
PYAPP_PROJECT_NAME=sasonica
PYAPP_PROJECT_VERSION=$VERSION
PYAPP_DISTRIBUTION_PATH=/work/python.tar.gz
PYAPP_DISTRIBUTION_PYTHON_PATH=python/bin/python3
PYAPP_DISTRIBUTION_SITE_PACKAGES_PATH=python/lib/python$PY_MINOR/site-packages
PYAPP_FULL_ISOLATION=1
PYAPP_SKIP_INSTALL=1
PYAPP_EXEC_MODULE=sasonica
PYAPP_PASS_LOCATION=1
EOF
compile='set -a; . "$W/pyapp.env"; set +a; cd "$W/pyapp" && cargo build --release -q && cp target/release/pyapp "$W/sasonica"'
if command -v cargo >/dev/null && [ -z "${SASONICA_BUILD_IN_CONTAINER:-}" ]; then
  # CI (in rust:1-bullseye, or a Mac): compiled where the files are.
  sed -i.bak "s|^PYAPP_DISTRIBUTION_PATH=.*|PYAPP_DISTRIBUTION_PATH=$WORK/python.tar.gz|" "$WORK/pyapp.env"
  W=$WORK bash -c "$compile"
else
  engine=$(command -v podman || command -v docker) || { echo "build: needs cargo, podman or docker" >&2; exit 1; }
  "$engine" run --rm -v "$WORK:/work:Z" -v sasonica-cargo:/usr/local/cargo/registry \
    -e W=/work docker.io/library/rust:1-bullseye bash -c "$compile"
fi
cp "$WORK/sasonica" "$OUT"
chmod +x "$OUT"

step "Done"
ls -lh "$OUT"
