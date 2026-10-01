#!/bin/sh
# uninstall.sh removes everything install.sh added and restarts AdGuard Home
# with the stock UI.  AdGuard Home's own files are left as they are.

set -eu

AGH_DIR=${AGH_DIR:-/opt/AdGuardHome}

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

systemctl disable --now agh-ui-sync.timer 2>/dev/null || true
rm -f /etc/systemd/system/agh-ui-sync.service /etc/systemd/system/agh-ui-sync.timer
rm -f /etc/systemd/system/AdGuardHome.service.d/dashboard-range.conf
rmdir /etc/systemd/system/AdGuardHome.service.d 2>/dev/null || true
rm -f /usr/local/bin/agh-ui-sync.sh /etc/default/agh-ui-sync "$AGH_DIR/agh-launch.sh"
rm -rf "$AGH_DIR/build"
systemctl daemon-reload
systemctl restart AdGuardHome
echo "Removed.  AdGuard Home is running with the stock UI."
