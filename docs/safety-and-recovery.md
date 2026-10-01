# Safety and recovery

## Installation and commands

Install the checksum-verified `agh-patcher-tools.tar.gz` from the `ui-<AdGuard Home version>` GitHub Release, following the [README](../README.md). Its `TOOLS.json` records the exact compatible version, tooling revision and hashes of every installation file. The installer verifies those files, records the tooling revision and establishes the CLI/launcher without downloading a frontend or restarting AdGuard Home. Rerunning it preserves the configured paths and installed frontend. Docker needs no host systemd service.

`agh-patcher status` reads local version, frontend/tooling revisions and frontend health without contacting GitHub or recording availability. `agh-patcher check` reads the compatible public release and compares frontend and tooling independently, without changing installation state. `sudo agh-patcher update` explains the verified change and requires `yes`; `--yes` explicitly authorizes it without a prompt. A matching healthy frontend is a no-op, even if tooling differs. Update tooling by downloading/verifying the released tools and rerunning the installer.

`sudo agh-patcher uninstall` removes the CLI, launcher, patcher configuration/state, managed frontend and recorded rollback material. Native uninstall removes its service override and verifies the stock service before deleting frontend files. Docker must first be recreated without patcher mounts; uninstall refuses while those mounts remain. AdGuard Home's binary, YAML, work/config/query data and unrelated service overrides are preserved. Unrecognized frontend directories are retained. The downloaded tools directory is left for the administrator to remove.

No host timer or scheduled checker is installed. GitHub's scheduled builds publish release assets only.

## Managed Docker override

The Docker installer writes only `/opt/adguardhome-patcher/compose.patcher.yaml`, using the verified release template, configured Compose service and UI/launcher paths. The JSON-form YAML is deterministic and needs no host YAML library. The container argument identifies the running container; the optional second argument identifies its Compose service. The service defaults to its Compose label, then its container name, and an explicit name must agree with an existing Compose service label.

Setup records the generated file's SHA-256 and a configuration fingerprint carried by the container as a label. `status` and `check` verify the file; `check` reports tooling current only when its release revision, managed file, running image ID and container fingerprint align. Template/configuration changes require an explicit Compose recreation. Updating only the manager can retain the same override fingerprint without recreating the container. Tooling installation never restarts the container.

The override does not define `command` or inject update-check flags. The launcher passes administrator arguments through unchanged. Because replacing an entrypoint clears the image command in Compose, setup records the base container's immutable image ID and effective command, including an explicitly empty array; a generated entrypoint supplies those captured arguments only when Compose supplies no arguments. After changing the Docker image or base service command, recreate with the base Compose file only, rerun the compatible released Docker installer, then explicitly reapply the managed override before frontend updates. Image ID drift is reported as stale; nothing is applied automatically. The base Compose file is not parsed or edited. Do not edit the managed override; preserve additional administrator flags/settings in the base file. If the managed file is modified or an unowned file occupies its path, preserve/move that file before reinstalling. Uninstall refuses while patcher mounts remain and removes an unchanged managed override; a modified regular file is retained instead of deleting possible administrator content.

## Integrity and compatibility

The CLI requires HTTPS for metadata, assets and redirects. Only the expected frontend archive/checksum URLs for the configured repository and exact compatibility tag are accepted. Downloads are bounded and use a private temporary directory. The updater verifies the published checksum, rejects unsafe paths, duplicates, links, special files, unexpected entries and excessive sizes, then checks the manifest version, every file hash and the independently recomputed frontend revision. It copies regular files itself and never executes downloaded scripts.

The launcher enables `--local-frontend` only when `build/VERSION` matches the running binary and a frontend index is present. A mismatch uses the stock UI. A missing compatible release fails preflight without changing the installation.

Before confirmation, updates leave the live frontend, settings and services untouched. Declining, including EOF on non-interactive stdin, changes nothing. After confirmation, an exclusive operation lock serializes installation/update/uninstall. The configuration, binary version and frontend are checked again after the prompt. Root-owned paths, permission checks and symlink rejection constrain privileged writes. Docker updates verify the configured parent UI mount, so replacement builds remain visible inside the container.

## Durability and rollback

The updater stages a complete verified build beside the live frontend. It flushes staged files, child directories, the stage root and its parent; it also flushes the existing build. JSON writes flush their temporary file before rename, then fsync the containing directory. A durable `prepared` transaction journal precedes live changes.

The updater renames the live build to a unique backup, then renames the stage into place. Each rename fsyncs the UI parent before the durable `backup_saved` or `installed` phase. These are two directory renames with a possible short gap, not an atomic multi-directory transaction.

The service/container is restarted and must pass three consecutive running-state checks, including Docker health when configured. The updater verifies the binary version and frontend hashes. It durably records the success receipt and `committed` phase before obsolete-backup cleanup and journal removal. The last successful backup is retained under the UI parent and recorded in `/var/lib/agh-patcher/last-update.json`.

A failed swap, restart, validation or commit records `rolling_back`, restores the prior build and receipt, and verifies the original service. The restoration rename is directory-fsynced before the durable `rolled_back` phase and journal removal. First-install failure returns to stock UI. Rollback accounts for a rename that succeeds before its fsync fails. Recovery failure returns an error and preserves available journal/backup/staging material. Frontend rollback never copies or restores AdGuard Home configuration or data.

## Interrupted updates

Power loss or SIGKILL can interrupt the renames or restart. `/var/lib/agh-patcher/transaction.json` records the exact build/stage/backup paths and last committed phase. `status` reports the interruption, and further updates refuse to proceed. Recovery requires administrator action.

CLI recovery messages link to these instructions at the immutable source commit recorded in the installed release's `TOOLS.json`. The tools bundle does not need a local documentation directory.

Stop AdGuard Home and inspect both the recorded paths and journal phase; the phase can lag a completed rename. For `prepared`, `backup_saved`, `installed` or `rolling_back`, restore the retained working backup where needed, or remove a partial first-install build to use stock UI. For `committed`, the build and receipt were flushed after validation: inspect them before deciding whether to keep or restore the build. For `rolled_back`, verify the restored build/service. Restart and verify the service, retain recovery material until confirmed, then remove the journal and flush its parent directory, for example with `sync` after explicit removal. A stage left before journal creation cannot have changed the live frontend.

Durability relies on Linux storage/filesystems honoring file and directory `fsync`. Unsupported flushes fail the operation. These checks establish process stability and file integrity; they do not perform an authenticated dashboard/DNS transaction on the administrator's service. The release gate separately smoke-tests the official binary and HTTP APIs in a temporary installation. See [development details](development.md) for release identities and validation.
