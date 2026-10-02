#!/bin/sh
# Bootstrap installer for adguardhome-patcher.
#
# Usage: install.sh native
#        install.sh docker <container-name> [compose-service-name]
#
# Detects the installed AdGuard Home version, downloads agh-patcher-tools.tar.gz
# and its SHA-256 file from the matching ui-<version> GitHub Release, verifies
# the archive, extracts it into /opt/adguardhome-patcher and runs the released
# installer from it. The released bundle is the installation payload; this
# script installs no frontend, restarts nothing and runs no Compose command.
set -eu
umask 022
LC_ALL=C
export LC_ALL

repository=rdna897/adguardhome-patcher
tools_dir=/opt/adguardhome-patcher
asset=agh-patcher-tools.tar.gz
version_pattern='v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9]+([.-][A-Za-z0-9]+)*)?'

usage() {
	printf '%s\n' 'usage: install.sh native' \
		'       install.sh docker <container-name> [compose-service-name]' >&2
	exit 2
}

fail() {
	printf 'agh-patcher install: %s\n' "$1" >&2
	exit 1
}

valid_name() {
	case $1 in
	'' | [!A-Za-z0-9]* | *[!A-Za-z0-9_.-]*) return 1 ;;
	esac
}

mode=${1-}
case $mode in
native)
	[ $# -eq 1 ] || usage
	;;
docker)
	[ $# -eq 2 ] || [ $# -eq 3 ] || usage
	container=$2
	service=${3-}
	valid_name "$container" || fail "invalid Docker container name: $container"
	[ $# -eq 2 ] || valid_name "$service" || fail "invalid Compose service name: $service"
	;;
*)
	usage
	;;
esac

[ "$(id -u)" = 0 ] || fail 'run as root, e.g. with sudo'

for tool in curl tar gzip sha256sum mktemp python3; do
	command -v "$tool" >/dev/null 2>&1 || fail "required command not found: $tool"
done
if [ "$mode" = docker ]; then
	command -v docker >/dev/null 2>&1 || fail 'required command not found: docker'
fi

if [ "$mode" = native ]; then
	if [ "${AGH_DIR+set}" = set ] && [ -z "$AGH_DIR" ]; then
		fail 'AGH_DIR is set but empty'
	fi
	agh_dir=${AGH_DIR:-/opt/AdGuardHome}
	case $agh_dir in /*) ;; *) fail "AGH_DIR must be an absolute path: $agh_dir" ;; esac
	binary=$agh_dir/AdGuardHome
	[ -f "$binary" ] && [ -x "$binary" ] \
		|| fail "AdGuard Home binary not found at $binary; set AGH_DIR for a custom directory"
	output=$("$binary" --version) \
		|| fail "cannot read the AdGuard Home version from $binary"
else
	output=$(docker exec "$container" /opt/adguardhome/AdGuardHome --version) \
		|| fail "cannot read the AdGuard Home version from running Docker container $container"
fi

# Same accepted format as the patcher: "... version vX.Y.Z[-pre]" then whitespace or end.
version=$(printf '%s\n' "$output" \
	| sed -n 's/^\(.*[^A-Za-z0-9_]\)\{0,1\}version \(v[^[:space:]]*\).*$/\2/p' | head -n 1)
printf '%s\n' "$version" | grep -Eqx "$version_pattern" \
	|| fail 'could not determine a valid installed AdGuard Home version'

release_url=https://github.com/$repository/releases/download/ui-$version
printf 'AdGuard Home %s: installing patcher tools from %s\n' "$version" "$release_url"

work=$(mktemp -d "${TMPDIR:-/tmp}/agh-patcher-bootstrap.XXXXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM

download() {
	curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
		--connect-timeout 20 --max-time 300 --retry 3 --retry-delay 2 \
		--max-filesize 10485760 -o "$work/$1" "$release_url/$1" \
		|| fail "download failed: $release_url/$1 (no published ui-$version release for this AdGuard Home version?)"
}
download "$asset"
download "$asset.sha256"

# The checksum file must be exactly the single line published by the release workflow.
[ "$(wc -l <"$work/$asset.sha256")" -eq 1 ] \
	&& grep -Eqx "[0-9a-f]{64}  $asset" "$work/$asset.sha256" \
	|| fail 'invalid published checksum file'
expected=$(cut -d ' ' -f 1 "$work/$asset.sha256")
actual=$(sha256sum <"$work/$asset" | cut -d ' ' -f 1)
[ "$actual" = "$expected" ] || fail "checksum mismatch for $asset; nothing was installed"
echo "Verified $asset SHA-256 $actual"

# Files outside the bundle (installed UI, managed Compose override) are kept.
mkdir -p "$tools_dir"
tar --no-same-owner --no-same-permissions -xzf "$work/$asset" -C "$tools_dir" \
	|| fail "extraction into $tools_dir failed; the released installer was not run"

installer=$tools_dir/install/$mode/install.sh
[ -f "$installer" ] || fail "released installer missing: $installer"
if [ "$mode" = native ]; then
	set --
else
	set -- "$container"
	[ -z "$service" ] || set -- "$container" "$service"
fi
sh "$installer" "$@" || fail "released installer failed: $installer"

if [ "$mode" = docker ]; then
	echo 'Next: apply the managed override with the Compose command shown above, using your base Compose file.'
fi
