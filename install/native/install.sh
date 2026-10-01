#!/bin/sh
# Explicit manual management; includes legacy scheduler cleanup.
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
exec python3 "$root/scripts/agh-patcher.py" install-native "$@"
