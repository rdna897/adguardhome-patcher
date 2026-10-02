# Development and release validation

The repository is for development and source distribution. Supported installations use the versioned tools bundle described in the [README](../README.md).

## Build

Requirements: Node.js 22, Python 3.9+, Git, curl, tar and sudo when the official binary smoke test needs elevated permissions.

```sh
BROWSER_TEST=1 scripts/build-release.sh v0.107.79 dist
```

The supported patch base is `patch/PATCH_BASE`. The gate checks validation regressions, temporary native/Docker management installations, the root `install.sh` bootstrap with fake downloads, patch application, applied-source whitespace, type checking, lint, frontend tests, production output, optional desktop/mobile browser tests and the official binary/HTTP smoke test. It generates and verifies three archives and their SHA-256 files:

- `agh-patcher-tools.tar.gz`: the two installers, Docker override, manager, launcher, release manifest module, licence and generated `TOOLS.json`. It contains no development files. Installation checks the version and every listed file hash without requiring the source tree.
- `agh-dashboard-range.tar.gz`: compiled frontend, compatibility version, manifest, notice and licence.
- `agh-dashboard-range-source.tar.gz`: corresponding patched upstream source and patcher source/build/install scripts for GPL distribution.

Archive verification recomputes the frontend revision, compares tools/source bytes to the build tree, checks the tools inventory and all three checksums, and rejects packaged host scheduler units.

## Release identities

The frontend artefact revision (`patch:` and manifest `patch_revision`) hashes actual `build/static` bytes. The manager rejects links, special files, non-UTF-8 paths, backslashes and control characters. It SHA-256 hashes each regular file, sorts relative POSIX paths by UTF-8 bytes, then hashes `agh-frontend-static-sha256-v1` followed by a newline and each path, NUL byte and 32-byte file digest. Timestamps, permissions and archive metadata are excluded. The manifest lives outside `build/static`. The updater independently recomputes the revision from extracted and installed files.

The frontend input fingerprint (`frontend-input:`) hashes the exact `FRONTEND_INPUTS` listed in `scripts/release-manifest.py`. CI uses it to decide whether to rebuild before output exists; installed hosts do not compare it. Builds use `/tmp/adguardhome-patcher-frontend-build`, refusing to reuse an existing directory, because webpack's CSS compilation hashes include the source path.

The tooling revision (`tooling:`) hashes `TOOLING_INPUTS`: installation files plus release/build orchestration. It is stored in `TOOLS.json`, the frontend manifest and the local configuration. The bundle manifest stores installation-file hashes independently, allowing the installer to verify released files without packaging CI or frontend source. The external bundle checksum protects the complete downloaded archive. README, documentation, tests and the root `install.sh` bootstrap do not affect either input fingerprint. The bootstrap is fetched from `main` and is not packaged; it only selects, verifies, extracts and runs the version-matched tools bundle, which remains the installed payload. Tooling updates never select a frontend replacement or restart.

`TOOLS.json` also records the build's source commit for immutable [recovery documentation](safety-and-recovery.md#interrupted-updates) links. Publication requires it to match the workflow commit. Docker setup generates its managed override from the released JSON-form YAML template and host configuration, records its exact hash, and adds a configuration fingerprint label. Tooling availability checks require the managed file and running container label to match, so a stale override cannot appear current. Reinstallation writes the override before committing the new configuration/revision and never invokes Compose or restarts the container.

## GitHub Actions

PR validation checks out the exact PR head with `contents: read` and no persisted credentials. It runs the browser-inclusive gate without publishing or modifying issues.

Trusted builds run only on this repository's `main` for push, schedule and workflow dispatch. Scheduled builds check upstream releases every six hours. A release is reused only when frontend inputs/tooling match and all six assets exist. The build gate verifies all archives; publication checks all checksums and frontend/tools identities before uploading. Notes record `patch:`, `frontend-input:`, `tooling:`, commit and run URL. A reused release has its assets replaced and target commit updated. Compatibility failures produce an `incompatible` issue instead of frontend publication.
