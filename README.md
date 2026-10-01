# AdGuard Home Patcher

[![build](https://github.com/rdna897/adguardhome-patcher/actions/workflows/build.yml/badge.svg)](https://github.com/rdna897/adguardhome-patcher/actions/workflows/build.yml)

AdGuard Home Patcher adds frontend-only dashboard improvements to the official AdGuard Home binary or Docker image. It does not modify the AdGuard Home backend, configuration, statistics database, or server Query Log.

**The patcher never installs updates automatically. Updates are applied only when explicitly requested by an administrator.** No update timer or scheduled checker is installed.

## Features

### Dashboard time ranges

- **Default**, **Last 1 / 6 / 12 / 24 hours**, **Today**, **Last 7 days**, **Last 30 days**, and **Custom** hour/day ranges.
- The selected range persists in the browser.
- Statistics use the existing AdGuard Home statistics API; no backend or binary patch is required.
- Ranges longer than the configured statistics retention period are disabled.
- **Today** starts at local midnight in the browser's time zone.
- Hourly ranges include the current partial hour and preceding full hours.
- A single bucket is rendered as a flat series with the same hourly tooltip at both ends.
- Compact two-column statistics charts on phone-sized screens.

### Live Query Log

- **Start Live View / Pause Live View** actions on the existing Query Log page, with the preference retained in the browser.
- Live mode polls the existing Query Log API roughly one second after each completed request; there is no WebSocket, backend push stream, or modified AdGuard Home binary.
- Existing domain/client search and response-status filters continue to work in Live mode.
- New DNS requests appear automatically while you remain at the newest entries.
- Scrolling away from the top freezes the visible rows so troubleshooting history does not move underneath you.
- Incoming entries are queued behind a **new queries — Show newest** action while you read older rows.
- **Show newest** returns to and reveals the newest entries; naturally scrolling back to the top also reveals queued arrivals.
- Client block/unblock row actions preserve queued arrivals and their pending indicator.
- **Clear view** is a secondary action available during Live mode. Its tooltip and screen-reader description explain that it clears only the displayed browser Live View and keeps the server Query Log/history.
- Hidden tabs suspend polling and resume safely when visible again.
- Request failures retain the current rows and retry after a five-second back-off.
- The browser keeps at most **500 displayed rows plus 500 queued arrivals**.
- Normal paused Query Log refresh and pagination remain available.
- Desktop and mobile layouts are covered by production-browser validation.

Live Query Log is deliberately a lightweight troubleshooting view rather than a lossless server-pushed stream. Each poll reads up to 100 newest matching records. Bursts exceeding that page size between polls, long request gaps, or time spent in a hidden tab can leave gaps in the live browser view. The authoritative server Query Log remains available through normal paused browsing and **Refresh**. See the [desktop and mobile control screenshots](docs/live-query-log-controls.md).

## Screenshots

The screenshots show a custom 13-hour range with sample statistics. `google.com` and `microsoft.com` are example blocked domains.

**Desktop**

![Desktop dashboard with a custom 13-hour range and example blocked domains](docs/dashboard-desktop.png)

**Mobile**

<img src="docs/dashboard-mobile.png" alt="Mobile dashboard viewport with a custom 13-hour range and two-column charts" width="390">

The mobile screenshot shows one visible screen. Scroll down in the dashboard to see the client and domain tables.

## How it works

GitHub Actions checks for the newest stable AdGuard Home release every six hours. It applies the frontend patch and runs validation regression checks, applied-source whitespace checks, type checking, lint, frontend tests, a production build, browser validation, and a smoke test against the official AdGuard Home binary. Successful builds are published as `ui-vX.Y.Z`; compatibility failures create an `incompatible` issue.

Pull requests to `main` validate the exact PR head with read-only permissions. The PR path does not publish releases, modify issues, or receive write-capable checkout credentials. Trusted release/publish jobs remain restricted to this repository's `main` branch and trusted push, schedule, or manual events.

On a Linux host, `agh-patcher` provides explicit status, check, update and uninstall commands. Updates require confirmation, exact AdGuard Home compatibility, the published SHA-256 checksum, and a validated frontend manifest. The launcher enables `--local-frontend` only when `build/VERSION` matches the AdGuard Home binary; otherwise AdGuard Home starts with its stock dashboard. GitHub's scheduled release builds publish assets only; they never install them on your host.

## Download the installer (no Git required)

For either native or Docker installation, download the repository archive on your **Linux host**. On Debian/Ubuntu:

```sh
sudo apt update
sudo apt install -y curl ca-certificates tar python3
patcher_archive=$(mktemp)
curl -fL --retry 2 https://github.com/rdna897/adguardhome-patcher/archive/refs/heads/main.tar.gz \
  -o "$patcher_archive"
sudo mkdir -p /opt/adguardhome-patcher
sudo tar -xzf "$patcher_archive" --strip-components=1 -C /opt/adguardhome-patcher
rm -f "$patcher_archive"
```

Then follow the native or Docker steps below. Extracting this archive also works over an existing patcher installation and preserves its downloaded `ui/` directory. Archive extraction does not remove obsolete files: rerunning the installer removes the two known legacy `install/systemd/agh-ui-sync.timer` and `.service` templates as well as the installed automatic updater. Git is needed only if you want to develop or build the patch yourself.

## Native installation

These instructions add the patched dashboard to an **existing native AdGuard Home installation on Linux with systemd**. For installing AdGuard Home itself, follow the official AdGuard Home getting-started documentation.

The default binary directory is `/opt/AdGuardHome`, and the service is `AdGuardHome.service`. After downloading the installer above, run:

```sh
sudo sh /opt/adguardhome-patcher/install/native/install.sh
```

For a different binary directory:

```sh
sudo env AGH_DIR=/your/AdGuardHome sh /opt/adguardhome-patcher/install/native/install.sh
```

Requires Python 3.9 or newer. The installer copies the launcher beside the binary, installs `/usr/local/bin/agh-patcher`, writes `/etc/agh-patcher/config.json`, and adds a systemd override preserving the service's startup arguments. It disables/stops/removes any legacy `agh-ui-sync.timer` and service, removes the old updater/settings and obsolete source-tree unit templates, and reloads systemd. It does not download a frontend, enable a timer, or restart AdGuard Home. Rerunning it preserves an existing compatible frontend and upgrades the tooling revision separately.

Check status and explicitly install/update the frontend:

```sh
agh-patcher status
agh-patcher check
sudo agh-patcher update
sudo journalctl -u AdGuardHome -n 30 --no-pager
```

Type `yes` at the update prompt to apply the verified frontend and restart AdGuard Home. The packaged native manager targets Linux/systemd; it does not support unattended updates or custom scheduler/service-manager restart commands.

## Docker installation

Use the official `adguard/adguardhome` image and retain your existing **work/configuration volumes, ports, networking, and other required command flags**. The host runs the downloader; the container receives a read-only UI directory and launcher. The container does not need download tools or Docker socket access.

The following assumes Docker Compose on a Linux host with systemd, an existing container named `adguardhome`, and a Compose service named `adguardhome`.

### 1. Install the manual host tooling

After downloading the installer above, run on the Docker host:

```sh
sudo sh /opt/adguardhome-patcher/install/docker/install.sh adguardhome
```

Replace the final `adguardhome` with your container name. The script reads that container's version, prepares `/opt/adguardhome-patcher/ui`, and installs the CLI and launcher at `/usr/local/lib/adguardhome-patcher/agh-launch.sh`. It retires legacy updater units and does not install a timer, download a frontend or restart the container.

### 2. Add the dashboard override

From the directory containing your existing Compose file:

```sh
sudo docker compose -f compose.yaml \
  -f /opt/adguardhome-patcher/install/docker/compose.override.yaml \
  up -d adguardhome
```

Use your actual Compose filename. If the service name differs, edit the service key in the override and use that name in the command. The override preserves the base image, volumes, ports, and networking while changing the working directory/startup wrapper. Retain any additional command flags required by your installation.

The override mounts the **parent UI directory**, allowing later atomic UI replacements to become visible inside the container. The launcher verifies the running binary version before enabling the patched dashboard.

A minimal base Compose example for a new installation is:

```yaml
services:
  adguardhome:
    image: adguard/adguardhome:latest
    container_name: adguardhome
    restart: unless-stopped
    ports:
      - "53:53/tcp"
      - "53:53/udp"
      - "3000:3000/tcp"
      - "8080:80/tcp"
    volumes:
      - ./work:/opt/adguardhome/work
      - ./conf:/opt/adguardhome/conf
```

Start the base file, finish AdGuard Home setup on port 3000, then apply the two steps above. Port 8080 maps the dashboard when AdGuard Home listens on container port 80. Add encrypted-DNS or DHCP ports as required. Existing installations should always reuse their existing data paths.

Check the installation:

```sh
agh-patcher status
agh-patcher check
sudo agh-patcher update
sudo docker logs --tail 30 adguardhome
```

Apply the Compose override before updating: the CLI checks that the configured parent UI directory is mounted at `/opt/adguardhome/ui`. Before the first patch installation, the launcher uses the stock UI. After the confirmed update, look for `agh-launch: using patched dashboard UI for vX.Y.Z`. One CLI configuration targets one native service or one Docker container.

## Updates

### Update the patched dashboard

For an existing native or Docker installation, run these on the **Linux host**:

```sh
agh-patcher status
agh-patcher check
sudo agh-patcher update
```

`status` is local and read-only: it shows the binary version, installed frontend revision, installed tooling revision and frontend health, with availability marked unknown until you explicitly check. Older manager configurations show an unknown tooling revision until the installer is rerun. It does not contact GitHub or record a cache. `check` explicitly contacts the configured public GitHub release source and reports frontend **Up to date** or **Update available**, without replacing files, changing services or writing installation state. A newer release tooling revision alone does not indicate a frontend update.

`update` checks metadata for `ui-<installed AdGuard Home version>`, downloads the archive/checksum into a private temporary directory, verifies compatibility and all manifest file hashes, rejects unsafe tar paths/links/unexpected members, and explains the planned change. It performs no service restart or frontend mutation during preflight. Type `yes` to proceed; a declined prompt leaves the installation and services untouched. The deliberate non-interactive equivalent is `sudo agh-patcher update --yes`; do not schedule it if you require administrator-controlled updates.

Frontend and tooling revisions are separate content identities, independent of the release's publication date. The frontend revision hashes only `patch/PATCH_BASE`, `patch/dashboard-range.patch`, and the production recipe `scripts/frontend-build.sh`; the exact upstream version and its locked build dependencies are selected by the compatibility tag. README, docs, tests, installer and manager changes do not change this revision. Release metadata's `patch:` line, the UI manifest's `patch_revision`, and frontend receipts use this frontend identity. The separate `tooling:` identity covers management/install/release tooling and is recorded locally only by rerunning the installer; downloading a UI never upgrades the host command.

Same compatible frontend revision and healthy files means no frontend download/replacement/restart, even when newer tooling has been published; leftover legacy updater artefacts still trigger a cleanup confirmation. Changed frontend build inputs indicate an update. Missing/damaged files can be repaired with the same verified revision. Older releases without the new manifest/revision are refused until rebuilt with current tooling. If the installed AdGuard Home version has no compatible release, the update is refused and the existing installation remains intact; the launcher uses the stock UI when its patch version does not match.

After confirmation, the CLI retires any legacy automatic updater, stages the complete verified build beside the live one, flushes its files/directories and commits a recovery journal before replacing the previous build through two same-filesystem directory renames. Each rename and journal phase is directory-fsynced. It restarts the service/container, verifies stable running state and frontend hashes, and durably records success before removing the journal. A failed swap, restart or validation restores the previous build and retries the original service; a first-install failure returns to the stock UI. Abrupt interruption requires manual inspection/recovery. These Linux durability boundaries depend on filesystem/storage honouring `fsync`; the two renames are not one atomic transaction. See [exact safety, durability and recovery limitations](docs/manual-updates.md).

Hard-refresh the dashboard after an update. On mobile, close and reopen the tab if it still shows the old UI. A dashboard-only update does not require reinstalling AdGuard Home or pulling a new Docker image.

### Update the host scripts

Existing installations must rerun the new installer to retire their legacy timer; downloading a frontend alone cannot migrate host tooling. Until you do this, an older installed sync script/timer retains its old behaviour.

Repeat **Download the installer (no Git required)** above to replace the patcher's files, then rerun the installer for your existing installation:

```sh
# Native: use the same AGH_DIR as your original installation, if customized.
sudo sh /opt/adguardhome-patcher/install/native/install.sh
# Or Docker: use your existing container name.
sudo sh /opt/adguardhome-patcher/install/docker/install.sh adguardhome
```

Run only the command for your installation. This updates the tooling revision and removes known obsolete source-tree timer/service templates without downloading a frontend or restarting AdGuard Home. If you customized the Compose override, retain those settings when downloading the new files; the archive replaces the supplied override. AdGuard Home data volumes and the downloaded `ui/` directory are preserved.

### Update AdGuard Home

Update native AdGuard Home normally, then explicitly run `agh-patcher check` and `sudo agh-patcher update` for its new version. If no compatible patch exists, the launcher uses the stock UI. No timer installs a replacement later.

For Docker, pull and recreate the service using **both Compose files**:

```sh
sudo docker compose -f compose.yaml \
  -f /opt/adguardhome-patcher/install/docker/compose.override.yaml pull adguardhome
sudo docker compose -f compose.yaml \
  -f /opt/adguardhome-patcher/install/docker/compose.override.yaml up -d adguardhome
sudo agh-patcher update
```

Update the image tag in the base file first if you pin versions. Refresh the browser after a dashboard update.

If a build is missing for an older supported AdGuard Home version, use **Actions → build → Run workflow** with that version tag.

## Uninstall

For a native installation:

```sh
sudo sh /opt/adguardhome-patcher/install/native/uninstall.sh
```

For an installation with the new tooling, use `sudo agh-patcher uninstall` and confirm with `yes` (or explicitly pass `--yes`). The shell uninstaller above also supports legacy installations without the CLI; use `sudo env AGH_DIR=/your/AdGuardHome sh ...` for a legacy custom binary directory.

Uninstall removes the CLI, launcher, patcher settings/state, patcher-owned frontend and recorded rollback material, native override, and legacy updater timer/service/settings/scripts. It reloads systemd, restarts native AdGuard Home with its stock UI and verifies that it remains running. AdGuard Home's binary, configuration/data, and unrelated administrator service overrides are preserved. An unrecognized frontend directory is retained rather than deleted. The downloaded installer/source directory is not an installed service and may be removed separately.

For Docker, first recreate the container with the **base Compose file only** so the original entrypoint is restored:

```sh
sudo docker compose -f compose.yaml up -d --force-recreate adguardhome
sudo agh-patcher uninstall
```

The CLI refuses Docker uninstall while patcher mounts remain. Work/configuration volumes are retained; after base-only recreation, uninstall removes the installed manual tooling, host frontend and legacy units. For a legacy installation without the CLI, use `sudo sh /opt/adguardhome-patcher/install/docker/uninstall.sh` from the newly downloaded installer.

## Building and validation

The current patch base is defined by `patch/PATCH_BASE`. Building requires Node.js 22, Python 3, git, curl, tar, and sudo when the smoke test needs elevated permissions.

```sh
scripts/build-release.sh v0.107.79
```

For the complete browser-inclusive gate:

```sh
BROWSER_TEST=1 scripts/build-release.sh v0.107.79
```

The gate verifies patch application, validation regressions, staged and unstaged applied-source whitespace, type checking, lint, frontend tests, production webpack output, desktop/mobile Live Query Log behaviour, and the official AdGuard Home binary smoke test. Release output includes the patched UI archive and checksum plus a corresponding source archive containing the patched upstream source, licence, patch, and build/install scripts.

The gate also runs `python3 scripts/tests/test-patcher.py` against temporary native/Docker installations and mock services/network, including independent identities, no-op tooling-only updates, migration cleanup and transaction durability ordering. It never modifies a real AdGuard Home service. `scripts/release-manifest.py` supplies separate frontend and tooling revisions to release metadata; `build/MANIFEST.json` records the frontend identity, build tooling provenance, exact compatibility version and frontend file hashes. Publishing tooling changes refreshes source assets without making existing healthy frontends appear outdated. Archive verification checks every revision input is included and matches the repository.

Browser validation covers action names, keyboard activation/focus, persisted Live preferences, browser-only clearing, singular/plural pending actions, and control spacing at 1440, 390, and 320 pixels wide, alongside the existing polling/state regressions. Set `LIVE_LOG_SCREENSHOT_DIR=/path/to/screenshots` when running the browser-inclusive gate to save viewport captures of inactive, Live, and queued-query states.

To edit the frontend patch:

```sh
git clone --depth 1 --branch "$(cat patch/PATCH_BASE)" https://github.com/AdguardTeam/AdGuardHome.git agh
cd agh
git apply ../patch/dashboard-range.patch
# Edit client/..., then:
git add -N client
git diff --binary -- client ':!client/node_modules' > ../patch/dashboard-range.patch
```

Blank unified-patch context lines require a space, so `patch/dashboard-range.patch` has a narrowly scoped `blank-at-eol` exemption. The build independently checks both staged and unstaged **applied source** with Git's trailing-space, blank-EOF, and space-before-tab checks. Regression tests verify malformed applied source fails validation.

## Licence

[GPL-3.0](LICENSE), matching AdGuard Home. This project modifies the frontend and is not an official AdGuard product. Upstream copyright notices are preserved in the source archives.
