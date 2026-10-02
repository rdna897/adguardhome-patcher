#!/bin/sh
# Bootstrap only: install the exact-version released tools, then hand off.
set -eu

fail() {
	printf '%s\n' "$*" >&2
	exit 1
}

usage() {
	printf 'Usage:\n  install.sh native\n  install.sh docker <container-name> [compose-service-name]\n' >&2
	exit 1
}

valid_name() {
	case "$1" in
		''|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*) fail "Invalid Docker container/service name: $1" ;;
	esac
}

case "${1-}" in
	native) [ "$#" -eq 1 ] || usage ;;
	docker)
		[ "$#" -eq 2 ] || [ "$#" -eq 3 ] || usage
		valid_name "$2"
		[ "$#" -eq 2 ] || valid_name "$3"
		;;
	*) usage ;;
esac
mode=$1
shift

[ "$(id -u)" -eq 0 ] || fail 'Run this installer with sudo/root.'
for command in curl tar sha256sum python3 install mktemp grep; do
	command -v "$command" >/dev/null 2>&1 || fail "Required command not found: $command"
done

if [ "$mode" = docker ]; then
	command -v docker >/dev/null 2>&1 || fail 'Required command not found: docker'
	version_output=$(docker exec "$1" /opt/adguardhome/AdGuardHome --version) \
		|| fail 'Cannot determine the installed AdGuard Home version.'
else
	version_output=$("${AGH_DIR:-/opt/AdGuardHome}/AdGuardHome" --version) \
		|| fail 'Cannot determine the installed AdGuard Home version.'
fi
agh_version=$(printf '%s\n' "$version_output" | python3 -c '
import re, sys
match = re.search(r"\bversion (v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?)(?:\s|$)", sys.stdin.read())
if not match:
    sys.exit(1)
print(match.group(1))
') || fail 'Invalid or missing AdGuard Home version.'

tools_root=/opt/adguardhome-patcher
[ ! -L /opt ] && [ ! -L "$tools_root" ] || fail 'Refusing a symlinked tools directory.'
umask 077
tools_dir=$(mktemp -d)
trap 'rm -rf "$tools_dir"' 0
trap 'exit 1' HUP INT TERM
cd "$tools_dir"
release_url="https://github.com/rdna897/adguardhome-patcher/releases/download/ui-$agh_version"
for asset in agh-patcher-tools.tar.gz agh-patcher-tools.tar.gz.sha256; do
	curl --proto '=https' --proto-redir '=https' -fL --retry 2 "$release_url/$asset" -o "$asset"
done
# Require an archive entry, so a checksum file for another file cannot pass.
grep -Eq '^[a-f0-9]{64} [ *]agh-patcher-tools\.tar\.gz$' agh-patcher-tools.tar.gz.sha256 \
	|| fail 'Invalid tools archive checksum file.'
sha256sum -c agh-patcher-tools.tar.gz.sha256
install -d -m 755 "$tools_root"
tar --no-same-owner --no-same-permissions -xzf agh-patcher-tools.tar.gz -C "$tools_root"
# The released installer validates its manifest and installation state.
sh "$tools_root/install/$mode/install.sh" "$@"
