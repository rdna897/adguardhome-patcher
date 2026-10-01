#!/bin/sh
# Install the version-matched dashboard and restart the native service or container.
# Settings come from SYNC_CONFIG (default /etc/default/agh-ui-sync) or the environment.
# MODE: native (default) or docker. GITHUB_REPO defaults to rdna897/adguardhome-patcher.
# Native: AGH_DIR defaults to /opt/AdGuardHome; RESTART_CMD can override the restart.
# Docker: DOCKER_CONTAINER defaults to adguardhome; set AGH_UI_ROOT to the host UI directory.

set -eu

SYNC_CONFIG=${SYNC_CONFIG:-/etc/default/agh-ui-sync}
[ ! -f "$SYNC_CONFIG" ] || . "$SYNC_CONFIG"
GITHUB_REPO=${GITHUB_REPO:-rdna897/adguardhome-patcher}
MODE=${MODE:-native}
AGH_DIR=${AGH_DIR:-/opt/AdGuardHome}
DOCKER_CONTAINER=${DOCKER_CONTAINER:-adguardhome}
RESTART_CMD=${RESTART_CMD:-systemctl restart AdGuardHome}
ASSET=agh-dashboard-range.tar.gz

log() { echo "agh-ui-sync: $*" >&2; }

case "$MODE" in
native)
	AGH_UI_ROOT=${AGH_UI_ROOT:-$AGH_DIR}
	version_output=$("$AGH_DIR/AdGuardHome" --version)
	;;
docker)
	AGH_UI_ROOT=${AGH_UI_ROOT:-/opt/adguardhome-patcher/ui}
	version_output=$(docker exec "$DOCKER_CONTAINER" /opt/adguardhome/AdGuardHome --version)
	;;
*) log "MODE must be native or docker"; exit 1 ;;
esac
version=$(printf '%s\n' "$version_output" | sed -n 's/.*version \(v[^ ]*\).*/\1/p')
[ -n "$version" ] || { log "couldn't read the AdGuard Home version"; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

fetch() {
	name=$1
	dest=$2
	code=$(curl -sSL --retry 2 --connect-timeout 15 --max-time 120 -o "$dest" -w '%{http_code}' \
		"https://github.com/$GITHUB_REPO/releases/download/ui-$version/$name")
	case "$code" in
	200) return 0 ;;
	404) return 1 ;;
	*) log "download of $name failed with HTTP $code"; exit 1 ;;
	esac
}

if ! fetch "$ASSET.sha256" "$tmp/$ASSET.sha256"; then
	log "no patched UI for $version yet; the launcher uses the stock dashboard"
	exit 0
fi
want=$(cut -d ' ' -f 1 "$tmp/$ASSET.sha256")
have=$(cat "$AGH_UI_ROOT/build/SHA256" 2>/dev/null || true)
if [ "$want" = "$have" ] && [ "$(cat "$AGH_UI_ROOT/build/VERSION" 2>/dev/null)" = "$version" ]; then
	exit 0
fi

fetch "$ASSET" "$tmp/$ASSET" || { log "release ui-$version has no $ASSET"; exit 1; }
(cd "$tmp" && sha256sum -c "$ASSET.sha256" >/dev/null) || { log "checksum mismatch, not installing"; exit 1; }
mkdir -p "$tmp/x"
tar xzf "$tmp/$ASSET" -C "$tmp/x"
[ "$(cat "$tmp/x/build/VERSION")" = "$version" ] || { log "archive is for another version"; exit 1; }
[ -f "$tmp/x/build/static/index.html" ] || { log "archive has no index.html"; exit 1; }

# Swap inside the UI directory so a Docker parent-directory bind mount sees new files.
dst="$AGH_UI_ROOT/build"
mkdir -p "$dst"
rm -rf "$dst/static.new" "$dst/static.old"
cp -r "$tmp/x/build/static" "$dst/static.new"
[ ! -d "$dst/static" ] || mv "$dst/static" "$dst/static.old"
mv "$dst/static.new" "$dst/static"
printf '%s\n' "$version" >"$dst/VERSION"
printf '%s\n' "$want" >"$dst/SHA256"
for notice in LICENSE.txt NOTICE; do
	[ ! -f "$tmp/x/build/$notice" ] || cp "$tmp/x/build/$notice" "$dst/$notice"
done
rm -rf "$dst/static.old"
log "installed the patched UI for $version, restarting AdGuard Home"
if [ "$MODE" = docker ]; then
	docker restart "$DOCKER_CONTAINER" >/dev/null
else
	$RESTART_CMD
fi
