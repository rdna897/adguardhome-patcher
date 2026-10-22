# Development and release validation

The repository is for development and source distribution. Supported installations use the versioned tools bundle described in the [README](../README.md).

## Build

Requirements: Node.js 22, Python 3.9+, Git, curl and tar. The official-binary test runs without elevated permissions. Browser checks use the existing Playwright Chromium/dependency setup.

```sh
BROWSER_TEST=1 scripts/build-release.sh v0.107.79 dist
```

The supported patch base is `patch/PATCH_BASE`. The gate checks validation regressions, temporary native/Docker management installations, the root `install.sh` bootstrap with fake downloads, patch application, applied-source whitespace, type checking, lint, frontend tests, production output, optional desktop/mobile browser tests and the official-binary DNS/Query Log integration test, including HTTP smoke checks. It generates and verifies three archives and their SHA-256 files:

- `agh-patcher-tools.tar.gz`: the two installers, Docker override, manager, launcher, release manifest module, licence and generated `TOOLS.json`. It contains no development files. Installation checks the version and every listed file hash without requiring the source tree.
- `agh-dashboard-range.tar.gz`: compiled frontend, compatibility version, manifest, notice and licence.
- `agh-dashboard-range-source.tar.gz`: corresponding patched upstream source plus the patcher build/release/install sources for the released artefacts, for GPL distribution. It is not a complete repository mirror.

Archive verification recomputes the frontend revision, compares tools/source bytes to the build tree, checks the tools inventory and all three checksums, and rejects packaged host scheduler units.

## Real Query Log integration

The binary stage downloads the official release once and passes that unchanged binary to `scripts/tests/querylog-real-api.cjs`. It retains the frontend-serving and `stats?recent` smoke checks, and tests real DNS traffic, HTTP cursor pagination, and the production Stage 1 recovery helper. `SMOKE_TEST=0` explicitly skips this stage; PR validation does not set it and always runs the integration test.

The test writes a complete disposable configuration before startup, avoiding the first-run installer and its root requirement. Web and both DNS listeners bind only to `127.0.0.1` on dynamically reserved non-privileged ports; port 53 and host DNS settings are never used. The work/configuration, credentials and all query history belong to a new temporary directory. A random username and a fixed test-only password authenticate this instance. Filtering lists, client reverse lookups, WHOIS, host-file lookups and update checks are disabled. A wildcard rewrite answers a unique `.invalid` marker with `192.0.2.123`; configured upstream, bootstrap, fallback and private resolvers point only to a local fail-closed DNS trap. Any upstream traffic fails the test. Rate limiting and the DNS cache are disabled for this instance.

Every DNS request is sent over a localhost UDP socket and must receive a matching, successful A response. Serial requests have 1ms separation to avoid depending on timestamp ties. A 64-record query-log buffer with file logging enabled exercises both memory and file-backed reads, without inserting or altering log records. Bounded API readiness checks wait for every expected unique domain, rather than sleeping for asynchronous log writes.

The test generates **1,352 genuine queries**: one checkpoint anchor, 251 burst records, then 1,100 overflow records. The isolated 251-record search must return data pages of **100, 100 and 51**, newest first, without duplicates or omissions. It follows the actual `oldest` cursor until exhaustion: v0.107.79 returns a nonempty cursor on the short final page, then an empty page/cursor. Every request uses `limit=100` and the marker search, never `offset`; the primary scan uses `response_status=all`. Cursors retain their complete RFC3339Nano precision and timezone offset; an exact boundary request verifies exclusive `older_than` behaviour.

The locked frontend build dependencies bundle the actual recovery helper and normalizer for Node; the test contains no alternate recovery algorithm. The 251 arrivals plus anchor recover in three requests without a gap. The overflow exhausts the production ten-page budget, reports `page-budget`, and rebases to a single-page next poll. A final API scan verifies all 1,352 records and timestamps remain unchanged. With `BROWSER_TEST=1`, a focused production-browser scenario uses this same real server and confirms normal synchronisation, the persistent gap warning and one-page polling after rebase. An online availability event triggers the rebase cycle; the assertion requires its observable Catching up to gap-only status transition before checking the request count, proving recovery finished rather than rechecking the previous cycle's warning. Its network guard rejects external origins and the server-clear endpoint; it does not mock Query Log responses.

The 251-record burst is also paginated with the effective `response_status=rewritten` filter: its rows and cursors must match the `all` scan exactly, while `response_status=blocked` must return no records. A separate rewritten-filter baseline and production-helper catch-up verify that every recovery request preserves that filter. Every nonempty page must return its final record's exact timestamp as `oldest`, including sub-millisecond precision. Recovery checks also assert that the supplied checkpoint is never mutated. Limiting the same three-page burst to two requests must return exactly the newest 200 rows, report a `page-budget` gap and rebase subsequent polling to one request; it cannot claim to have reached the older anchor.

HTTP requests have five-second deadlines, DNS responses two seconds, readiness checks thirty seconds, scans twenty pages, and the entire disposable test a 120-second watchdog. Failure diagnostics include missing domains, page/cursor details and the binary's recent output. Cleanup runs on success, exceptions, SIGINT, SIGTERM and SIGHUP, closes the browser, stops the child (with a three-second kill fallback), closes sockets and removes its temporary directory. The gate's existing trap removes the downloaded binary. The test requires no production credentials, volumes, services or additional dependencies.

Exact nanosecond ties cannot be forced reliably through the unchanged binary's clock. The real test checks strict timestamp exclusion; the existing deterministic unit tests separately cover tied boundaries and indistinguishable events. Other [Query Log API limitations](live-query-log-controls.md#api-limits), including missing event IDs and the absence of a stable snapshot, still apply.

In v0.107.79, [`searchFiles` limits each file scan to 50,000 candidates](https://github.com/AdguardTeam/AdGuardHome/blob/v0.107.79/internal/querylog/search.go), including nonmatching records. Consequently a sparse filter can return an empty page with a continuation cursor, and `oldest` may identify a scanned nonmatching record. The integration's 1,352-record dataset does not reach that scan cap; deterministic recovery tests cover sparse and empty continuing pages. Neither a short page nor an empty data array alone proves exhaustion. Candidate selection also precedes response sorting, so the serial-query fixture establishes ordinary pagination without claiming a stable snapshot under concurrent or delayed writes.

## Release identities

The frontend artefact revision (`patch:` and manifest `patch_revision`) hashes actual `build/static` bytes. The manager rejects links, special files, non-UTF-8 paths, backslashes and control characters. It SHA-256 hashes each regular file, sorts relative POSIX paths by UTF-8 bytes, then hashes `agh-frontend-static-sha256-v1` followed by a newline and each path, NUL byte and 32-byte file digest. Timestamps, permissions and archive metadata are excluded. The manifest lives outside `build/static`. The updater independently recomputes the revision from extracted and installed files.

The frontend input fingerprint (`frontend-input:`) hashes the exact `FRONTEND_INPUTS` listed in `scripts/release-manifest.py`. CI uses it to decide whether to rebuild before output exists; installed hosts do not compare it. Builds use `/tmp/adguardhome-patcher-frontend-build`, refusing to reuse an existing directory, because webpack's CSS compilation hashes include the source path.

The tooling revision (`tooling:`) hashes `TOOLING_INPUTS`: installation files plus release/build orchestration. It is stored in `TOOLS.json`, the frontend manifest and the local configuration. The bundle manifest stores installation-file hashes independently, allowing the installer to verify released files without packaging CI or frontend source. The external bundle checksum protects the complete downloaded archive. README, documentation, tests and the root `install.sh` bootstrap do not affect either input fingerprint. Tooling updates never select a frontend replacement or restart.

The root `install.sh` is a convenience bootstrap fetched from [`main`](https://raw.githubusercontent.com/rdna897/adguardhome-patcher/main/install.sh), outside both `agh-patcher-tools.tar.gz` and `agh-dashboard-range-source.tar.gz`. It is absent from `TOOLS_FILES` and `TOOLING_INPUTS`, so changing the bootstrap alone does not create an installed tooling update. It detects the installed version, selects and downloads the matching tools bundle, verifies its checksum, extracts it and hands off to its released installer. The versioned tools bundle remains the installed payload and trust/compatibility boundary.

`TOOLS.json` also records the build's source commit for immutable [recovery documentation](safety-and-recovery.md#interrupted-updates) links. Publication requires it to match the workflow commit. Docker setup generates its managed override from the released JSON-form YAML template and host configuration, records its exact hash, and adds a configuration fingerprint label. Tooling availability checks require the managed file and running container label to match, so a stale override cannot appear current. Reinstallation writes the override before committing the new configuration/revision and never invokes Compose or restarts the container.

## GitHub Actions

PR validation checks out the exact PR head with `contents: read` and no persisted credentials. It runs the browser-inclusive gate without publishing or modifying issues.

Trusted builds run only on this repository's `main` for push, schedule and workflow dispatch. Scheduled builds check upstream releases every six hours. A release is reused only when frontend inputs/tooling match and all six assets exist. The build gate verifies all archives; publication checks all checksums and frontend/tools identities before uploading. Notes record `patch:`, `frontend-input:`, `tooling:`, commit and run URL. A reused release has its assets replaced and target commit updated. Compatibility failures produce an `incompatible` issue instead of frontend publication.
