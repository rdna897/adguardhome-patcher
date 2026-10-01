#!/bin/sh
# agh-launch.sh starts AdGuard Home with the patched dashboard UI, but only
# when that UI was built for exactly the installed binary version.  Otherwise
# it starts the stock UI embedded in the binary.  All arguments are passed
# through to AdGuard Home unchanged.
#
# Environment:
#   AGH_BIN      Path to the AdGuardHome binary.  Default: next to this script.
#   AGH_UI_ROOT  Directory containing build/static and build/VERSION.
#                Default: the binary's directory.

set -u

script_dir=$(cd "$(dirname "$0")" && pwd)
AGH_BIN=${AGH_BIN:-"$script_dir/AdGuardHome"}
AGH_UI_ROOT=${AGH_UI_ROOT:-$(dirname "$AGH_BIN")}

version=$("$AGH_BIN" --version 2>/dev/null | sed -n 's/.*version \(v[^ ]*\).*/\1/p')
stamp=$(cat "$AGH_UI_ROOT/build/VERSION" 2>/dev/null || true)

if [ -n "$version" ] \
	&& [ "$version" = "$stamp" ] \
	&& [ -f "$AGH_UI_ROOT/build/static/index.html" ] \
	&& [ ! -e "$AGH_UI_ROOT/build/DISABLED" ]; then
	echo "agh-launch: using patched dashboard UI for $version" >&2
	# AdGuard Home reads the local frontend from ./build/static.
	cd "$AGH_UI_ROOT" || exit 1
	exec "$AGH_BIN" --local-frontend "$@"
fi

echo "agh-launch: patched UI not available for '${version:-unknown}' (built for '${stamp:-none}'), using stock UI" >&2
exec "$AGH_BIN" "$@"
