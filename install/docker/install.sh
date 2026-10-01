#!/bin/sh
# Install the host sync timer for an existing official AdGuard Home container.
# Usage: sudo sh install/docker/install.sh [container-name] [owner/repo]
set -eu
DOCKER_CONTAINER=${1:-adguardhome}
repo=${2:-rdna897/adguardhome-patcher}
root=$(cd "$(dirname "$0")/../.." && pwd)
AGH_UI_ROOT=${AGH_UI_ROOT:-/opt/adguardhome-patcher/ui}
[ "$(id -u)" = 0 ] || { echo 'run as root' >&2; exit 1; }
command -v curl >/dev/null || { echo 'curl is required on the host' >&2; exit 1; }
docker exec "$DOCKER_CONTAINER" /opt/adguardhome/AdGuardHome --version >/dev/null
mkdir -p "$AGH_UI_ROOT" /usr/local/lib/adguardhome-patcher
install -m 0755 "$root/scripts/agh-launch.sh" /usr/local/lib/adguardhome-patcher/agh-launch.sh
install -m 0755 "$root/scripts/agh-ui-sync.sh" /usr/local/bin/agh-ui-sync.sh
umask 077
cat >/etc/default/agh-ui-sync <<EOF
MODE=docker
GITHUB_REPO='$repo'
DOCKER_CONTAINER='$DOCKER_CONTAINER'
AGH_UI_ROOT='$AGH_UI_ROOT'
EOF
umask 022
install -m 0644 "$root/install/systemd/agh-ui-sync.service" "$root/install/systemd/agh-ui-sync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agh-ui-sync.timer
/usr/local/bin/agh-ui-sync.sh
echo 'Host sync installed. Apply install/docker/compose.override.yaml to enable the launcher.'
