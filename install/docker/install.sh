#!/bin/sh
# Install released manual management tooling.
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
exec python3 "$root/scripts/agh-patcher.py" install-docker "$@"
