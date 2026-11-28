# Local Docker development and verification

The application now has a Docker build and Compose definition. Cloudflare is a
later deployment stage. The runtime image contains the real Python service,
built React UI and dedicated Chromium. It is separate from the optional
Docker-based tool for executing scripts, which remains disabled.

## Prerequisites and present verification boundary

Install a Docker-compatible CLI, a running Linux-container Engine and Docker
Compose v2 with support for optional `env_file` entries. On Windows, a normal
route is [Docker Desktop with its WSL 2 backend](https://docs.docker.com/desktop/setup/install/windows-install/).
Follow its current Windows/virtualization/WSL requirements. Installation, OS
feature changes and any required reboot are owner actions; this setup does not
perform them automatically. Run these checks before application setup:

```powershell
docker version
docker compose version
```

`docker version` must show both Client and Server information for Linux
containers. A CLI alone is insufficient. During implementation the development
terminal had no Docker command on PATH, so no successful local container run
is claimed. The PR workflow builds and exercises the images on its Linux Docker
runner. The workflow must actually pass before treating the container gate as
verified; source/config tests do not substitute for it.

## Run the real application locally

From the repository root:

```powershell
Copy-Item .docker.env.example .docker.env
# Fill .docker.env locally, using your editor.
docker compose build app
docker compose up -d app
docker compose ps
docker compose logs --tail 100 app
```

Set `OPENAI_API_KEY`, a random `HARNESS_TOKEN` of at least 24 characters and a
different stable random `CREDENTIAL_KEY` of at least 24 characters. Keep those
values out of chat, Git and image layers. Add Google/provider configuration only
for accounts you actually intend to connect. The real app refuses to start
without valid configuration; it never silently substitutes the synthetic test
runner. `.docker.env` is ignored by Git and rejected by the tracked-private-file
guard. `.dockerignore` allows only explicit build inputs, excluding live state,
credentials, local tools and local configuration.

Open `http://127.0.0.1:8787` and enter the harness access token. The Google OAuth
callback remains `http://127.0.0.1:8787/oauth/google/callback`; register that
exact URI with Google. The Compose mapping intentionally uses the same port
inside and outside the container. Stop an existing host service on that port
first. Do not change just one side of the mapping without updating the public
callback and Host configuration.

```powershell
docker compose stop app
docker compose start app
# Recreate the container while retaining its named state volume:
docker compose up -d --force-recreate app
```

The named `assistant-state` volume stores `/data/.assistant`, including SQLite,
encrypted credentials, workspace files and the dedicated browser profile.
Normal container recreation retains that volume. Deleting the volume loses
state; no cleanup command deleting application volumes is part of this guide.
Do not mount the everyday browser profile or the owner's home directory.

## Persistent local database directory

For repeatable local testing, `compose.local.yaml` can bind-mount a stable
database directory independently of the existing state volume. Both the real
server and standalone runtime honor `ASSISTANT_DATABASE_DIR`; by default they
continue using the original state directory. Credentials, artifacts and the
dedicated browser profile remain in the named state volume.

Run the following inside Linux/WSL. Use a **native persistent Linux filesystem**
such as ext4 for the live database. A WSL filesystem can itself be backed by a
VHDX on a local Windows drive. Do not put the active WAL database on a Windows
shared `/mnt/c` or `/mnt/d` directory, a network share or a cloud-synced folder.
SQLite's database, WAL and shared-memory siblings must stay together; see
[SQLite WAL requirements](https://www.sqlite.org/wal.html).

For an existing local app, preserve its original volume and migrate while it is
stopped. The helper refuses an existing destination, checks integrity and table
counts, and leaves the original database in place:

```bash
docker compose stop app
sudo python3 scripts/local-database.py migrate \
  --database-dir /var/lib/personal-agent-harness/database
export HARNESS_DATABASE_DIR=/var/lib/personal-agent-harness/database
docker compose -f compose.yaml -f compose.local.yaml up -d --no-build app
```

Build the updated image before migration. For an automatic machine-local
override, copy `compose.local.yaml` to the ignored `compose.override.yaml` and
set `HARNESS_DATABASE_DIR` in an ignored `.env` file. Then normal `docker compose`
commands retain the mount. Keep using the override after migration: the original
named volume's SQLite file is a rollback copy, not the current database.

Inspect the live database through Linux/container tools. For desktop inspection,
export a consistent standalone SQLite snapshot while the app is running:

```bash
sudo python3 scripts/local-database.py snapshot \
  --database-dir /var/lib/personal-agent-harness/database \
  --output-dir /mnt/d/projects/personal-agent-harness/.assistant/db-snapshots
```

Snapshots include integrity/checksum manifests and use unique names. A private
native copy is retained in the database directory's `snapshots/` folder before
export; the Windows destination uses its existing Windows directory ACL. They do not
include workspace/browser/vault files or the separate encryption key and are
**not a complete workspace backup**. Open the exported snapshot, not the active
database, in a Windows desktop editor. Do not run a second app against the live
directory. Machine-specific overrides, `.env`, databases and snapshots remain
outside Git and image build inputs.

## Network and runtime boundaries

- Host mode still binds only `127.0.0.1`. Only explicit `CONTAINER_MODE=true`
  changes the internal listen address to `0.0.0.0`.
- Compose publishes the port on host `127.0.0.1` only. The app remains a local,
  single-owner service; this is not hosted login or multiuser authentication.
- Request guards retain exact loopback Host authorities. Optional
  `HARNESS_ALLOWED_HOSTS` is a comma-separated list of exact `host:port`
  authorities, accepted only in container mode. Wildcards, URLs and out-of-range
  ports are rejected. Forwarded host/protocol headers are not trusted.
- `MCP_TRUSTED_LOCAL_URLS` accepts comma-separated owner-configured endpoints;
  the MCP registry validates literal loopback URLs. Inside a container,
  `127.0.0.1` refers to that container, not the host. This does not turn arbitrary
  LAN addresses or remote servers into trusted local services.
- The image runs as UID/GID 10001 with a read-only root filesystem in Compose,
  a writable state volume, bounded temporary space and shared memory, dropped
  capabilities and resource limits. No Docker socket is mounted.
- Chromium is installed from the locked Playwright version and uses only the
  dedicated application profile. Headless navigation/approval policy remains
  enforced. To sign in, open a public page in Browser, select its tab, then
  choose **Sign in manually**. The authenticated viewer is available through
  the app's loopback address. The assistant pauses browser access until you
  choose **End sign-in and return control**. After ten minutes, reconnect to
  continue. Ending sign-in closes the tabs and keeps persistent site sessions
  in the dedicated profile. No separate display or browser debugging port is
  published.
- Script execution stays off. Installing the app in Docker does not grant the
  model host-shell access or nested Docker administration. A future isolated
  execution broker must remain a separate authorization boundary.

The image pins Node/Python image-index digests and the uv bootstrap version;
application dependencies come from the committed npm/uv lockfiles. Refresh image
digests and OS packages deliberately with image scans and CI. A digest pin is
reproducibility, not proof that its contents have no vulnerabilities.

## Synthetic test profile

The `acceptance` build target adds test code and Node for the historical
Node-to-Python SQLite migration fixture. Neither the tests nor the synthetic
server entrypoint is present in the `runtime` image.

```powershell
docker compose --profile test build acceptance
docker compose --profile test run --rm acceptance
```

This profile receives no `.docker.env`, real credentials, network access or
application volume. It runs unit/component/ASGI tests, then all included
`ui_*acceptance.py` scenarios using Chromium, synthetic models and mocked
providers. Its temporary SQLite/workspace state is discarded with the test
container. The `/test-work` tmpfs is explicitly writable by the non-root UID.

For the complete deployment-image gate, run:

```powershell
python scripts/container-smoke.py --build
```

That host-side script requires Python 3 and a working Docker Engine. It builds
both image targets, checks that the runtime image is non-root and excludes test
fixtures/local config, runs the acceptance image without network, then starts a
uniquely named synthetic fixture container with its own named volume. It checks
API authentication and Host rejection, chat, exact procedure approval/replay,
scheduled completion, dedicated browser launch/private-destination rejection,
process restart and full container recreation over the same test volume. It
removes only fixture resources generated in that invocation. Existing app
containers and volumes are not targets of cleanup.

PR CI invokes this same image gate under the existing required backend check;
the frontend check also runs the local, Google and MCP UI scenarios. CI remains
triggered only by pull-request events, including updates to an open PR.

## Daily model usage

Set `DAILY_TOKEN_LIMIT=0` in `.docker.env` and recreate the app container to
disable the daily admission cap. Positive values keep the cap. The runtime
continues to record model usage and the Settings dialog displays the amount
charged. Provider quota and rate limits still apply.

## Backup and restore

Stop the app with `docker compose stop app` before a workspace backup. The
backup command uses SQLite's backup API and includes workspace artifacts and
encrypted connector state. It records the source build, schema, and checksums.
The dedicated Chromium profile is excluded. Sign in again after a restore.

Run the commands below with the backend environment for the original build.
For Docker, use Linux/WSL and paths for the stopped container's state mount and
native database mount. `docker inspect` reports these under `Mounts`. If the
database lives in the state volume, pass its `assistant.sqlite` there.

```sh
python -m harness.cli.workspace_backup backup \
  --state "$state_dir" --database "$database_file" \
  --archive "$backup_dir/workspace.zip" --build "$source_build"

python -m harness.cli.workspace_backup restore \
  --archive "$backup_dir/workspace.zip" --target "$new_restore_dir" \
  --build "$source_build"
```

Set `source_build` to the original Git commit or immutable image identifier.
Keep the archive and the original `CREDENTIAL_KEY` separately. Restore obtains
the key from the environment. Use the original `HARNESS_TOKEN` if the workspace
used it as the credential key. A successful restore reports SQLite integrity and
the number of credentials it could unlock offline.

Restore creates `.assistant/` and `database/` under a new directory. To test the
restored copy, start the original image in a separate Compose project on an
unused loopback port. Bind those directories to `/data/.assistant` and
`/database`, set `ASSISTANT_DATABASE_DIR=/database`, and make both writable by
UID 10001. Use an isolated test configuration with connector access disabled.
Review conversations, documents, schedules, and approval states before making
the restored copy active.

Before upgrading, retain a verified archive and its original image. Keep at
least the latest verified archive and the last pre-upgrade archive until the
new version passes a restore drill. To roll back, stop the upgraded app and use
the old image with a new restore of its matching archive. Keep the upgraded
data directory available until you have verified the rollback. Reconcile any
external actions that happened after the snapshot before resuming work.

## Cloudflare boundary

This local Docker setup is the execution/test baseline, not a deployment of the
current local disk to Workers. A final Workers-based deployment still needs its
durable state/lease/scheduler adapters, authenticated edge boundary, managed
runner/container wiring, artifact/credential persistence, external callback
configuration, observability and restoration checks. Temporary cloud container
disk cannot be the sole home of the SQLite database or credentials.
