#!/bin/sh
# Invoked by the operator's existing service manager; never copies or deploys data.
set -eu
umask 077
cd "$(dirname "$0")/.."
: "${DATA_DIR:?Set the isolated enterprise data directory}"
: "${JL_AI_BASE_URL:?Set authorized model endpoint}"
: "${JL_AI_MODEL:?Set authorized model}"
if [ -z "${JL_AI_API_KEY:-}" ] && [ -n "${BOT_MODEL_KEYCHAIN_SERVICE:-}" ]; then
    JL_AI_API_KEY="$(security find-generic-password -s "$BOT_MODEL_KEYCHAIN_SERVICE" -w)"
fi
: "${JL_AI_API_KEY:?Inject existing authorized API key at runtime}"
export DATA_DIR JL_AI_BASE_URL JL_AI_MODEL JL_AI_API_KEY
export BOT_STRICT_PILOT=1
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-3000}"
export COOKIE_SECURE="${COOKIE_SECURE:-1}"
exec "${PYTHON_BIN:-python3}" app.py serve
