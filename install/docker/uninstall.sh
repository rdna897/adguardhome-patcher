#!/bin/sh
# Remove the host sync after recreating the container without the dashboard override.
set -eu
[ "$(id -u)" = 0 ] || { echo 'run as root' >&2; exit 1; }
systemctl disable --now agh-ui-sync.timer 2>/dev/null || true
rm -f /etc/systemd/system/agh-ui-sync.service /etc/systemd/system/agh-ui-sync.timer
rm -f /usr/local/bin/agh-ui-sync.sh /etc/default/agh-ui-sync
systemctl daemon-reload
echo 'Host sync removed. Dashboard files remain in the host UI directory for reuse.'
