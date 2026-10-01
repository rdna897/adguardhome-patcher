#!/usr/bin/env bash
# build-release.sh applies the dashboard patch to an AdGuard Home release, runs
# the compatibility gate and packages the patched UI.
#
# Usage: scripts/build-release.sh <tag> [out-dir]
#
# On success, out-dir (default: dist) contains:
#   agh-dashboard-range.tar.gz         build/static and build/VERSION
#   agh-dashboard-range.tar.gz.sha256
#   agh-dashboard-range-source.tar.gz  patched source and build/install scripts
#   agh-dashboard-range-source.tar.gz.sha256
# On failure, it exits non-zero and writes the reason to out-dir/REASON.
#
# Gate: the stats API still documents "recent", the patch applies (three-way
# against PATCH_BASE), applied-source whitespace, typecheck, eslint, unit tests,
# production build, and a smoke test that runs the official binary of the
# release with the patched UI.  Set SMOKE_TEST=0 to skip the last step.

set -euo pipefail

tag=${1:?usage: build-release.sh <tag> [out-dir]}
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9]+([.-][A-Za-z0-9]+)*)?$ ]] \
	|| { echo "invalid AdGuard Home release tag: $tag" >&2; exit 1; }
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out=$(mkdir -p "${2:-dist}" && cd "${2:-dist}" && pwd)
patch_file="$repo_root/patch/dashboard-range.patch"
patch_base=$(cat "$repo_root/patch/PATCH_BASE")
REPO_URL=${REPO_URL:-https://github.com/AdguardTeam/AdGuardHome.git}
SMOKE_TEST=${SMOKE_TEST:-1}
BROWSER_TEST=${BROWSER_TEST:-0}

SUDO=
if [ "$(id -u)" != 0 ]; then
	SUDO=sudo
fi

work=$(mktemp -d)
trap '$SUDO rm -rf "$work"' EXIT
src="$work/agh"

log() { printf '::notice::%s\n' "$*" >&2; }

fail() {
	echo "$1" >"$out/REASON"
	printf '::error::%s: %s\n' "$tag" "$1" >&2
	exit 1
}

step() {
	local name=$1
	shift
	echo "::group::$name" >&2
	if ! "$@" >&2; then
		echo "::endgroup::" >&2
		fail "$name failed"
	fi
	echo "::endgroup::" >&2
}

host_arch() {
	case "$(uname -m)" in
	x86_64 | amd64) echo amd64 ;;
	aarch64 | arm64) echo arm64 ;;
	*) return 1 ;;
	esac
}

# smoke_test starts the official binary of the release with the patched UI and
# checks that the UI is served, stats accepts "recent", and Query Log supports
# the existing filtered, limited requests used for live frontend polling.
smoke_test() {
	local dir="$work/smoke" arch port pid ok=1 i
	arch=$(host_arch) || { echo "unsupported architecture, skipping"; return 0; }
	mkdir -p "$dir/work" "$dir/build"
	curl -fsSL -o "$dir/agh.tgz" \
		"https://github.com/AdguardTeam/AdGuardHome/releases/download/$tag/AdGuardHome_linux_$arch.tar.gz"
	tar xzf "$dir/agh.tgz" -C "$dir"
	cp -r "$src/build/static" "$dir/build/static"
	port=$((20000 + RANDOM % 20000))

	# A first launch refuses to start without root, and CI runners aren't
	# root, so use sudo there.
	(cd "$dir" && exec $SUDO ./AdGuardHome/AdGuardHome --local-frontend --no-check-update \
		-w "$dir/work" --web-addr "127.0.0.1:$port" >"$dir/agh.log" 2>&1) &
	pid=$!

	local base="http://127.0.0.1:$port"
	for i in $(seq 1 30); do
		curl -s -o /dev/null "$base/" && break
		sleep 1
	done

	local main_js
	main_js=$(grep -o 'main\.[a-f0-9]*\.js' "$src/build/static/index.html" | head -n 1)
	if curl -fsS -X POST "$base/control/install/configure" \
		-H 'Content-Type: application/json' \
		-d "{\"web\":{\"ip\":\"127.0.0.1\",\"port\":$port},\"dns\":{\"ip\":\"127.0.0.1\",\"port\":$((port + 1))},\"username\":\"smoke\",\"password\":\"smoke-test-password\"}"; then
		sleep 3
		curl -fsS -c "$dir/cj" -X POST "$base/control/login" \
			-H 'Content-Type: application/json' \
			-d '{"name":"smoke","password":"smoke-test-password"}' \
			&& curl -fsS -b "$dir/cj" "$base/" | grep -q "$main_js" \
			&& curl -fsS -b "$dir/cj" "$base/control/stats?recent=3600000" | grep -q num_dns_queries \
			&& curl -fsS -b "$dir/cj" "$base/control/querylog?limit=100&search=smoke&response_status=all" | grep -q '"data"' \
			&& ok=0
	fi

	$SUDO kill "$pid" 2>/dev/null || true
	wait "$pid" 2>/dev/null || true
	if [ "$ok" != 0 ]; then
		tail -n 30 "$dir/agh.log"
	fi

	return "$ok"
}

rm -f "$out/REASON" "$out"/agh-dashboard-range.tar.gz* "$out"/agh-dashboard-range-source.tar.gz*

step "validation regression tests" python3 "$repo_root/scripts/tests/test-validation.py"

log "building $tag with patch made against $patch_base"
git clone -q --depth 1 --branch "$tag" "$REPO_URL" "$src" || fail "couldn't fetch the $tag source"

grep -q "'name': 'recent'" "$src/openapi/openapi.yaml" \
	|| fail "the stats API no longer documents the 'recent' parameter"

grep -q "'/querylog':" "$src/openapi/openapi.yaml" \
    || fail "the release no longer documents the Query Log API"

# The base release lets git do a three-way merge when surrounding code moved.
git -C "$src" fetch -q --depth 1 origin tag "$patch_base" || true
git -C "$src" apply --3way --whitespace=nowarn "$patch_file" >&2 \
	|| fail "the patch doesn't apply cleanly"
if git -C "$src" diff --name-only --diff-filter=U | grep -q .; then
	fail "the patch has merge conflicts"
fi

# --3way stages the applied patch; checking only the working diff misses it.
step "applied-source whitespace" "$repo_root/scripts/check-applied-whitespace.sh" "$src"

mapfile -t ts_files < <(grep '^+++ b/client/' "$patch_file" | sed 's#^+++ b/client/##' | grep -E '\.tsx?$')

cd "$src/client"
step "npm ci" npm ci --no-audit --no-fund
step "typecheck" npm run typecheck
step "lint" npx eslint --ext .ts,.tsx "${ts_files[@]}"
step "unit tests" npx vitest --run
step "production build" npm run build-prod
[ -f "$src/build/static/index.html" ] || fail "build produced no index.html"

if [ "$BROWSER_TEST" = 1 ]; then
	if [ -z "${PLAYWRIGHT_CHROMIUM_EXECUTABLE:-}" ]; then
		step "install Chromium for browser checks" npx playwright install --with-deps chromium --only-shell
	fi
	step "Live Query Log browser checks" node "$repo_root/scripts/tests/live-query-log-browser.cjs" "$src"
fi

if [ "$SMOKE_TEST" = 1 ]; then
	step "smoke test against the official $tag binary" smoke_test
fi

echo "$tag" >"$src/build/VERSION"
cp "$src/LICENSE.txt" "$src/build/LICENSE.txt"
printf 'Modified AdGuard Home dashboard by adguardhome-patcher.\nUpstream: %s\nBuilt: %s\nSource: agh-dashboard-range-source.tar.gz in the same release.\n' \
	"$tag" "$(date -u +%F)" >"$src/build/NOTICE"
tar czf "$out/agh-dashboard-range.tar.gz" -C "$src" build/static build/VERSION build/LICENSE.txt build/NOTICE
(cd "$out" && sha256sum agh-dashboard-range.tar.gz >agh-dashboard-range.tar.gz.sha256)
mkdir -p "$src/patcher"
cp -r "$repo_root/patch" "$repo_root/scripts" "$repo_root/install" "$src/patcher/"
cp "$repo_root/LICENSE" "$repo_root/README.md" "$src/patcher/"
tar czf "$out/agh-dashboard-range-source.tar.gz" \
	--exclude='./.git' --exclude='./client/node_modules' --exclude='./build' -C "$src" .
(cd "$out" && sha256sum agh-dashboard-range-source.tar.gz >agh-dashboard-range-source.tar.gz.sha256)
log "$tag passed the compatibility gate"
