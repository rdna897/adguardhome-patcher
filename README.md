# AdGuard Home Patcher

Frontend improvements for the official AdGuard Home binary and Docker image. AdGuard Home configuration, DNS data and server Query Log stay intact.

## Features

- Dashboard time ranges: recent hours, today, days and custom ranges.
- Live Query Log with pause, queued arrivals, filters and browser-only clearing. [Screenshots](docs/live-query-log-controls.md).
- Manual updates only: no timer or scheduled host updater.
- Stock dashboard fallback when the installed frontend does not match AdGuard Home.

## Requirements

- An existing AdGuard Home installation on Linux.
- Python 3.9+, curl, CA certificates, tar and `sha256sum` on the host.
- Native: systemd service `AdGuardHome.service`.
- Docker: the official `adguard/adguardhome` image and Docker Compose.

## Install

Install from the [versioned GitHub Releases](https://github.com/rdna897/adguardhome-patcher/releases). The repository and source archive are for development/source distribution.

Choose the release matching your **installed AdGuard Home version**. Check it with `/opt/AdGuardHome/AdGuardHome --version` (native) or `docker exec adguardhome /opt/adguardhome/AdGuardHome --version` (Docker). Set `AGH_VERSION` below to that version, including the `v` prefix.

Download, verify and extract the released tools on the host:

```sh
AGH_VERSION=v0.107.79
(
  set -eu
  printf '%s\n' "$AGH_VERSION" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9]+([.-][A-Za-z0-9]+)*)?$'
  tools_dir=$(mktemp -d)
  trap 'rm -rf "$tools_dir"' EXIT
  cd "$tools_dir"
  release_url="https://github.com/rdna897/adguardhome-patcher/releases/download/ui-$AGH_VERSION"
  curl -fL --retry 2 "$release_url/agh-patcher-tools.tar.gz" -o agh-patcher-tools.tar.gz
  curl -fL --retry 2 "$release_url/agh-patcher-tools.tar.gz.sha256" -o agh-patcher-tools.tar.gz.sha256
  sha256sum -c agh-patcher-tools.tar.gz.sha256
  sudo install -d -m 755 /opt/adguardhome-patcher
  sudo tar --no-same-owner --no-same-permissions -xzf agh-patcher-tools.tar.gz -C /opt/adguardhome-patcher
)
```

Run the installer for your installation below. It verifies the released files and exact version, installs the manual CLI and launcher, and preserves any installed frontend. It does not download a frontend or restart AdGuard Home.

### Native

```sh
sudo sh /opt/adguardhome-patcher/install/native/install.sh
agh-patcher check
sudo agh-patcher update
```

The binary defaults to `/opt/AdGuardHome`. For a custom directory, run the installer with `sudo env AGH_DIR=/your/AdGuardHome sh /opt/adguardhome-patcher/install/native/install.sh`.

### Docker

Run the installer on the host, then apply its managed override from your Compose directory:

```sh
sudo sh /opt/adguardhome-patcher/install/docker/install.sh adguardhome
sudo docker compose -f compose.yaml \
  -f /opt/adguardhome-patcher/compose.patcher.yaml up -d adguardhome
agh-patcher check
sudo agh-patcher update
```

The installer takes `<container-name> [compose-service-name]`; the service defaults to the container's Compose label, then its container name. For different names, use e.g. `install.sh agh-container dns` and `up -d dns`. Use your actual base Compose filename. The patcher owns the override; your base file and data volumes remain administrator-owned. The managed override does not replace the base service command or administrator Compose settings. Keep using both files for Docker operations.

## Update

```sh
agh-patcher status
agh-patcher check
sudo agh-patcher update
```

`status` is local and read-only. `check` reads the compatible release and reports frontend and tooling updates separately. `update` verifies the frontend and asks you to type `yes` before replacing files and restarting AdGuard Home; `--yes` explicitly skips the prompt. A tooling-only update does not replace or restart the frontend.

To update management tooling, repeat the release download/verification above and rerun your installer. Docker setup regenerates its override without restarting the container; if `check` reports it is not active, run the Compose command above explicitly. After updating/recreating the AdGuard Home Docker image using the base Compose file only, rerun the compatible released Docker installer and reapply the managed override before updating the frontend. Hard-refresh your browser after a frontend update.

## Uninstall

Native:

```sh
sudo agh-patcher uninstall
```

Docker: first recreate the container with its base Compose file only, then uninstall:

```sh
sudo docker compose -f compose.yaml up -d --force-recreate adguardhome
sudo agh-patcher uninstall
```

Confirm with `yes`. Uninstall restores stock UI and removes managed tooling/frontend files while keeping AdGuard Home configuration/data and unrelated overrides. Docker uninstall refuses while patcher mounts remain. The downloaded tools directory can be removed separately after uninstall.

## How it works

The patch changes only the frontend. Releases match the exact AdGuard Home version; downloads are checked against SHA-256 checksums and manifests. Frontend identity comes from the actual built files, independently of the tooling revision. Confirmed updates retain a rollback copy and durable transaction journal. No automatic updater is installed. [Safety and recovery details](docs/safety-and-recovery.md).

[Development and release validation](docs/development.md) · [GPL-3.0](LICENSE). This is not an official AdGuard product.
