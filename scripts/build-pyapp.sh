#!/usr/bin/env bash
# Builds an offline mzeval binary for one target triple with PyApp. The embedded Python already
# holds mozg-eval and its locked dependencies, so the binary never installs anything at run time.
# Usage: scripts/build-pyapp.sh <target-triple> [out-dir]; needs uv, cargo, curl and tar.
set -euo pipefail

PYAPP_VERSION=0.31.0
PBS_TAG=20261110
PYTHON_VERSION=3.13.18

target=${1:?usage: build-pyapp.sh <target-triple> [out-dir]}
root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "${2:-$root/dist}"
out=$(cd "${2:-$root/dist}" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

cd "$root"
version=$(uv version --short)
uv build --wheel --out-dir "$work/wheel"
uv export --frozen --no-dev --no-emit-project --output-file "$work/requirements.txt" >/dev/null

asset="cpython-${PYTHON_VERSION}+${PBS_TAG}-${target}-install_only_stripped.tar.gz"
pbs="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}"
curl -fsSL -o "$work/$asset" "$pbs/$asset"
curl -fsSL "$pbs/SHA256SUMS" | grep " ${asset}\$" > "$work/python.sha256"
(cd "$work" && sha256 -c python.sha256)
tar -xzf "$work/$asset" -C "$work"

# PyApp unpacks the archive as is, so its paths keep the python/ top folder.
case "$target" in
  *windows*) inner='python\python.exe'; site='python\Lib\site-packages'; exe=.exe ;;
  *) inner=python/bin/python3; site="python/lib/python${PYTHON_VERSION%.*}/site-packages"; exe= ;;
esac
python="$work/${inner//\\//}"
uv pip install --python "$python" --require-hashes --no-deps -r "$work/requirements.txt"
uv pip install --python "$python" --no-deps "$work"/wheel/*.whl
tar -czf "$work/distribution.tar.gz" -C "$work" python

mkdir "$work/pyapp"
curl -fsSL "https://github.com/ofek/pyapp/releases/download/v${PYAPP_VERSION}/source.tar.gz" \
  | tar -xz -C "$work/pyapp" --strip-components=1
(
  cd "$work/pyapp"
  export PYAPP_PROJECT_NAME=mozg-eval
  export PYAPP_PROJECT_VERSION="$version"
  export PYAPP_EXEC_SPEC=mozg_eval.cli:app
  export PYAPP_PYTHON_VERSION="${PYTHON_VERSION%.*}"
  export PYAPP_DISTRIBUTION_PATH="$work/distribution.tar.gz"
  export PYAPP_DISTRIBUTION_PYTHON_PATH="$inner"
  export PYAPP_DISTRIBUTION_SITE_PACKAGES_PATH="$site"
  export PYAPP_SKIP_INSTALL=1
  export PYAPP_FULL_ISOLATION=1
  cargo build --release --locked
)
cp "$work/pyapp/target/release/pyapp$exe" "$out/mzeval-$version-$target$exe"
echo "built $out/mzeval-$version-$target$exe"
