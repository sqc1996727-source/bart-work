#!/bin/sh
# Shared product source, independently configured enterprise instance.
set -eu
umask 077
cd "$(dirname "$0")/.."
: "${BOT_PROFILE:?Set the customer profile JSON}"
: "${DATA_DIR:?Set the independent customer data directory}"
test -f "$BOT_PROFILE" || { echo "Missing customer profile" >&2; exit 2; }
export BOT_PROFILE DATA_DIR BOT_STRICT_PILOT=1
# Never inherit the migration-only identity fallback.
unset BOT_LEGACY_COMPAT
exec /bin/sh scripts/run_pilot.sh
