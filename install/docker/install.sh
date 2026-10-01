#!/bin/sh
# Install released manual management tooling.
# Usage: install.sh <container-name> [compose-service-name]
# The service defaults to the container's Compose label, then its container name.
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
exec python3 "$root/scripts/agh-patcher.py" install-docker "$@"
