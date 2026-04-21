#!/usr/bin/env bash
#
# Regenerate gRPC stubs for Yandex SpeechKit v3 STT/TTS.
#
# Requires:
#   - Python 3.10+ with grpcio-tools installed (pip install -e .[dev])
#   - third_party/cloudapi checked out (git clone https://github.com/yandex-cloud/cloudapi.git third_party/cloudapi)
#
# Output: livekit/plugins/yandex/_proto/**

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLOUDAPI="${ROOT}/third_party/cloudapi"
GOOGLEAPIS="${CLOUDAPI}/third_party/googleapis"
OUT="${ROOT}/livekit/plugins/yandex/_proto"

if [[ ! -d "${CLOUDAPI}" ]]; then
  echo "Cloning yandex-cloud/cloudapi into ${CLOUDAPI}"
  git clone --depth 1 https://github.com/yandex-cloud/cloudapi.git "${CLOUDAPI}"
fi

PROTOS=(
  yandex/cloud/ai/stt/v3/stt.proto
  yandex/cloud/ai/stt/v3/stt_service.proto
  yandex/cloud/ai/tts/v3/tts.proto
  yandex/cloud/ai/tts/v3/tts_service.proto
  yandex/cloud/api/operation.proto
  yandex/cloud/operation/operation.proto
  yandex/cloud/validation.proto
)

# Clean previous generation (keep __init__.py at the top).
rm -rf "${OUT}/yandex" "${OUT}/google"
mkdir -p "${OUT}"

# Resolve proto sources: yandex/* from cloudapi, google/* from googleapis.
YC_PROTOS=()
GA_PROTOS=()
for p in "${PROTOS[@]}"; do
  case "$p" in
    yandex/*)  YC_PROTOS+=("${CLOUDAPI}/${p}") ;;
    google/*)  GA_PROTOS+=("${GOOGLEAPIS}/${p}") ;;
  esac
done

python -m grpc_tools.protoc \
  -I "${CLOUDAPI}" \
  -I "${GOOGLEAPIS}" \
  --python_out="${OUT}" \
  --grpc_python_out="${OUT}" \
  "${YC_PROTOS[@]}" ${GA_PROTOS[@]+"${GA_PROTOS[@]}"}

# Drop __init__.py at every package level so Python can import the stubs.
for base in "${OUT}/yandex" "${OUT}/google"; do
  [[ -d "${base}" ]] || continue
  find "${base}" -type d -exec bash -c 'touch "$0/__init__.py"' {} \;
done

# Rewrite absolute imports so the stubs resolve inside our private _proto namespace.
# Protoc emits `import yandex.cloud.ai...` / `from yandex.cloud.ai... import ...`
# which would collide with any other installed yandex package.
PREFIX="livekit.plugins.yandex._proto"
python - <<PY
import os, re, sys
root = "${OUT}"
prefix = "${PREFIX}"
pattern_import = re.compile(r'^import (yandex\.|google\.api\.)', re.M)
pattern_from = re.compile(r'^from (yandex\.|google\.api\.)', re.M)
for dp, _, files in os.walk(root):
    for f in files:
        if not f.endswith('.py'):
            continue
        path = os.path.join(dp, f)
        with open(path, 'r', encoding='utf-8') as fh:
            src = fh.read()
        new = pattern_import.sub(lambda m: f'import {prefix}.' + m.group(1), src)
        new = pattern_from.sub(lambda m: f'from {prefix}.' + m.group(1), new)
        if new != src:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(new)
PY

echo "Done. Stubs at ${OUT}"
