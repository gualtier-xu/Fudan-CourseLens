#!/usr/bin/env bash
# CourseLens macOS .app build (MAC-NIGHT-1 test build).
#
# Stages a faithful source-tree payload, bundles interpreter+deps with
# py2app, ad-hoc signs (no Developer ID path; see MAC-READINESS decision
# #2), zips with ditto, and runs structural leak gates before declaring
# the artifact usable. Gates fail the build; they never warn-and-continue.
#
# Usage: bash packaging/macos/build_macos_app.sh
# Outputs: dist/CourseLens-0.1.0-macos-<arch>.zip (+ .app, + SHA256SUMS.txt)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(python -c "import json;print(json.load(open('courselens-version.json'))['version'])" 2>/dev/null || echo "0.1.0")"
ARCH="$(uname -m)"
STAGING="${REPO_ROOT}/build/macos-staging"
PAYLOAD="${STAGING}/courselens"
APP_NAME="CourseLens.app"
ZIP_NAME="CourseLens-${VERSION}-macos-${ARCH}.zip"

cd "${REPO_ROOT}"

echo "== [1/6] Stage faithful source-tree payload =="
rm -rf "${STAGING}"
mkdir -p "${PAYLOAD}"
cp -R src "${PAYLOAD}/src"
cp -R frontend "${PAYLOAD}/frontend"
cp -R config "${PAYLOAD}/config"
cp -R shared "${PAYLOAD}/shared"
cp __init__.py courselens-version.json courselens_macos.py credentials.py path_utils.py "${PAYLOAD}/"
find "${PAYLOAD}" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "${PAYLOAD}" -name '*.pyc' -delete

echo "== [2/6] Install runtime + py2app =="
python -m pip install --requirement packaging/macos/requirements-macos.txt
python -m pip install py2app

echo "== [3/6] py2app build =="
COURSELENS_MACOS_STAGED_ROOT="${PAYLOAD}" python packaging/macos/setup.py py2app
test -d "dist/${APP_NAME}" || { echo "GATE FAIL: ${APP_NAME} missing"; exit 1; }

echo "== [4/6] Structural leak gates (fail-closed) =="
APP_BUNDLE="dist/${APP_NAME}"
test -d "${APP_BUNDLE}/Contents/Resources/courselens/frontend" \
  || { echo "GATE FAIL: frontend payload missing"; exit 1; }
test -f "${APP_BUNDLE}/Contents/Resources/courselens/path_utils.py" \
  || { echo "GATE FAIL: root modules missing from payload"; exit 1; }
test ! -e "${APP_BUNDLE}/Contents/Resources/courselens/runtime" \
  || { echo "GATE FAIL: runtime/ must never ship inside the .app"; exit 1; }
test ! -e "${APP_BUNDLE}/Contents/Resources/courselens/.local-secrets" \
  || { echo "GATE FAIL: secrets directory must never ship"; exit 1; }
test ! -e "${APP_BUNDLE}/Contents/Resources/courselens/worker" \
  || { echo "GATE FAIL: local worker is not part of the mac v1 payload"; exit 1; }
if du -sk dist | awk '{exit ($1 > 307200 ? 0 : 1)}'; then
  echo "GATE FAIL: bundle exceeds the 300MB size gate"
  exit 1
fi
if find "${APP_BUNDLE}" -type d \( -name 'pythonnet' -o -name 'clr_loader' \) | grep -q .; then
  echo "GATE FAIL: Windows-only loader modules must stay out of the mac bundle"
  exit 1
fi

echo "== [5/6] Ad-hoc sign + zip =="
codesign --force --deep --sign - "${APP_BUNDLE}"
ditto -c -k --sequesterRsrc --keepParent "${APP_BUNDLE}" "dist/${ZIP_NAME}"

echo "== [6/6] Checksums =="
cd dist
shasum -a 256 "${ZIP_NAME}" > SHA256SUMS.txt
cat SHA256SUMS.txt
echo "BUILD OK: dist/${ZIP_NAME}"
