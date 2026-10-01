#!/bin/sh
# Set up the dashboard patch for an existing native Linux/systemd installation.
# Usage: sudo sh install/native/install.sh [owner/repo]
set -eu
repo=${1:-rdna897/adguardhome-patcher}
root=$(cd "$(dirname "$0")/../.." && pwd)
AGH_DIR=${AGH_DIR:-/opt/AdGuardHome}
unit=AdGuardHome.service
[ "$(id -u)" = 0 ] || { echo 'run as root' >&2; exit 1; }
[ -x "$AGH_DIR/AdGuardHome" ] || { echo "AdGuard Home not found in $AGH_DIR (set AGH_DIR)" >&2; exit 1; }
command -v curl >/dev/null || { echo 'curl is required' >&2; exit 1; }

exec_start=$(systemctl cat "$unit" | sed -n 's/^ExecStart=\(.\{1,\}\)$/\1/p' | tail -n 1)
case "$exec_start" in
*"$AGH_DIR/AdGuardHome"*) launcher_start=$(printf '%s\n' "$exec_start" | sed "s#$AGH_DIR/AdGuardHome#$AGH_DIR/agh-launch.sh#") ;;
*"$AGH_DIR/agh-launch.sh"*) launcher_start=$exec_start ;;
*) echo "unexpected ExecStart in $unit: '$exec_start'" >&2; exit 1 ;;
esac
install -m 0755 "$root/scripts/agh-launch.sh" "$AGH_DIR/agh-launch.sh"
install -m 0755 "$root/scripts/agh-ui-sync.sh" /usr/local/bin/agh-ui-sync.sh
umask 077
cat >/etc/default/agh-ui-sync <<EOF
MODE=native
GITHUB_REPO='$repo'
AGH_DIR='$AGH_DIR'
EOF
umask 022
mkdir -p "/etc/systemd/system/$unit.d"
cat >"/etc/systemd/system/$unit.d/dashboard-range.conf" <<EOF
# Added by adguardhome-patcher.
[Service]
ExecStart=
ExecStart=$launcher_start
EOF
install -m 0644 "$root/install/systemd/agh-ui-sync.service" "$root/install/systemd/agh-ui-sync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agh-ui-sync.timer
/usr/local/bin/agh-ui-sync.sh
systemctl restart "$unit"
echo 'Dashboard patch installed. Check: journalctl -u AdGuardHome -n 30'
