# Home Assistant App: openHop Repeater

## About

This app runs openHop Repeater as a managed Home Assistant service.

All repeater settings are stored in one editable YAML file:

```text
/config/config.yaml
```

On the Home Assistant host, the file is available under:

```text
/addon_configs/*_openhop_repeater_main/config.yaml
```

The app creates the file on first start from the bundled openHop Repeater
configuration template. Unique admin and guest passwords plus a JWT signing
secret are generated for a new configuration. On later starts, missing settings
from the packaged template are merged into the existing file while user values
are preserved. If a merge
introduces missing main admin, guest password, or JWT secret fields, unique
values are generated before the file is published atomically. Security fields
already present in the user configuration are never silently changed by this
merge, except that an empty JWT secret is replaced before startup.

## Installation

1. Add the openHop Repeater repository to the Home Assistant app store.
2. Install **openHop Repeater**.
3. Disable **Protection mode** when using directly attached SPI, GPIO, USB, or
   serial radio hardware.
4. Start the app once to create `config.yaml`.
5. Stop the app and edit the generated file.
6. Start the app and select **Open Web UI**.

The initial configuration uses `radio_type: null`. This allows the service to
start before radio hardware has been configured.

## Configuration

`/config/config.yaml` is the only source of repeater settings. The single
Home Assistant app option, `branch_or_pr`, selects which upstream code runs
and is unrelated to the repeater configuration itself.

Review at least the following values before normal operation:

- `repeater.node_name`
- `repeater.security.admin_password`
- `repeater.security.guest_password`
- `radio_type`
- the configuration section for the selected radio backend
- radio frequency, bandwidth, spreading factor, coding rate, and transmit power
- identity, MQTT, companion, and policy settings used by the installation

The bundled template contains the configuration sections and comments supplied
with the openHop Repeater version included in the app image.

## Selecting an openHop Repeater branch or pull request

This app image is for testing upstream changes. The branch or pull request
to run is selected on the Home Assistant app **Configuration** tab:

- `branch_or_pr: main` runs the default branch.
- `branch_or_pr: dev` runs the `dev` branch.
- `branch_or_pr: 42` runs pull request `42` from
  `openhop-dev/openhop_repeater` (installed as `refs/pull/42/head`).

The requested source is installed on boot from
`https://github.com/openhop-dev/openhop_repeater.git`. An invalid value, or
a branch/PR that cannot be installed and verified, stops the app instead of
starting other code. The web-interface release-channel selector is ignored.

The installed source is stored in a persistent Python environment:

```text
/data/venv
```

The path is part of the app's private Home Assistant data directory. The
generated environment is retained only while it remains compatible with the
current app image and Python runtime, and it is reinstalled whenever the
configured branch/PR changes.

At startup, the app checks both installation metadata and the actual Python
import path. A source is considered active only when the requested ref
matches the verified installation in `/data/venv`. The verified ref is also
stored in a small marker file so startup still works if upstream metadata
cleanup removes a `direct_url.json` file.

To return to the default branch, set `branch_or_pr` back to `main` and
restart the app.

## Persistent storage

| Path | Contents |
|---|---|
| `/config/config.yaml` | User-editable openHop Repeater configuration |
| `/data/venv` | Python environment containing the installed branch/PR |
| `/data/venv/.openhop-ha-python` | App-image and Python compatibility marker for the environment |
| `/data/venv/.openhop-ha-branch` | Last source whose venv installation was verified by the app |
| `/var/lib/openhop_repeater` | Internal link to `/data` used by openHop Repeater |
| `/opt/openhop_repeater/venv` | Internal link to `/data/venv` used by the updater |

A legacy `/data/.update_channel` file left behind by older app versions is
ignored.

The `/data` directory is private to the app. Home Assistant removes it when
the app is uninstalled.

The generated virtual environment is excluded from app backups because it is
specific to the app image, packaged Python runtime and dependencies, system
architecture, and Python version. It is rebuilt from the configured
`branch_or_pr` app option when required.

The app image also contains a protected copy of its packaged `main` runtime
outside `/opt/openhop_repeater`, because the upstream updater removes source
trees from that directory before installing a branch. The image also preserves
the radio hardware and preset JSON files required by the first-run setup wizard
and exposes them beside packages installed into the persistent venv.

The user-editable configuration under `addon_configs` is managed by Home
Assistant and included with the app backup. Whether it is removed during
uninstallation depends on the configuration-removal choice made in Home
Assistant.

## Networking

The app uses host networking. openHop Repeater companion identities can
listen on ports defined in `config.yaml`, so the complete port set cannot be
declared statically in the app metadata.

The web interface listens on port `8000` by default. The Home Assistant
**Open Web UI** action uses this port. When `http.port` is changed, open the
configured port directly in a browser.

## Hardware access

Supported transports include:

- local SPI/GPIO radios;
- USB radio adapters;
- serial KISS modems;
- TCP modems;
- companion services using local network ports.

The app requests full hardware access and mounts the host udev database for
device discovery. Home Assistant only grants `full_access` when **Protection
mode** is disabled, so disable it for directly attached SPI, GPIO, USB, or
serial devices. TCP modems and other network-only connections do not require
that hardware access.

Network-based radio connections generally require less host-specific
configuration than direct hardware access.

For Raspberry Pi SPI hardware, enable SPI in the host boot configuration and
use BCM GPIO numbering in `config.yaml`.

## Updates

There are two independent update paths:

- **openHop Repeater code** is selected with the `branch_or_pr` app option
  and installed on boot.
- **Home Assistant app updates** are installed from Home Assistant and update
  the container image, startup scripts, bundled default version, and packaged
  configuration template. Missing template settings are merged into the user
  configuration on the next start.

Changing the repeater code does not update the Home Assistant app itself.

## Backups

The app uses cold backups so the service is stopped while its persistent data
is captured. The generated `/data/venv` directory is excluded and is rebuilt
after restore when necessary.

Home Assistant includes the configuration file under `addon_configs` with the
app backup.

## Troubleshooting

### The selected branch/PR is not active

Check the app log for entries similar to:

```text
selected source: ...; active source: ...
runtime package: ...
```

The installed source should load from `/data/venv`. Startup logs report the
resolved package path rather than trusting installation metadata alone.

### A branch/PR cannot be installed

Source installation requires:

- working DNS;
- HTTPS access to GitHub;
- a valid branch name or an existing pull-request number;
- a ref that can be installed by `pip` for the current architecture.

A failed installation stops the app instead of starting other code. Review
the complete installation error in the app log before retrying.

### The app does not start after editing `config.yaml`

The app validates the YAML syntax before starting openHop Repeater. Correct
the reported error and restart the app. The configuration file is not reset
automatically.

### The Web UI link opens the wrong port

The Home Assistant link targets port `8000`. When `http.port` uses another
value, open `http://<home-assistant-host>:<configured-port>/` directly.

### Directly attached hardware is unavailable

Confirm that:

- **Protection mode** is disabled;
- the required host interface is enabled;
- the device and GPIO values in `config.yaml` match the host;
- no other service is using the same hardware.

## License

The Home Assistant app files are licensed under the MIT License. See the
repository root `LICENSE` file. openHop Repeater remains subject to its upstream
license.
