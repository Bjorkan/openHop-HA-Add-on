# openHop Repeater Home Assistant App

This repository provides a Home Assistant app for
[openHop Repeater](https://github.com/openhop-dev/openhop_repeater).

The app runs the repeater as a managed Home Assistant service while keeping
all repeater settings in a single YAML file. Missing packaged defaults are
merged without replacing user values. The upstream branch or pull request to
run is selected with the `branch_or_pr` app option on the Configuration tab
and installed on boot.

## Installation

1. Add this repository to the Home Assistant app store.
2. Install **openHop Repeater**.
3. Start the app once to create the configuration file.
4. Stop the app and edit `/addon_configs/*_openhop_repeater_main/config.yaml`.
5. Start the app and open its web interface.

Directly attached SPI, GPIO, USB, or serial hardware requires **Protection
mode** to be disabled in the app settings. Network-only operation can remain
protected.

## Configuration

The complete openHop Repeater configuration is stored in:

```text
/addon_configs/*_openhop_repeater_main/config.yaml
```

Inside the app, the same file is available as:

```text
/config/config.yaml
```

Home Assistant app options select which upstream code runs (`branch_or_pr`,
for example `main` or `42`, plus the optional `core_branch_or_pr` override
for `openhop-dev/openhop_core`). The YAML file is the only source of
repeater settings.

## Branch / PR selection

Set `branch_or_pr` on the app Configuration tab to a branch name such as
`main` or to a pull-request number such as `42` from
`openhop-dev/openhop_repeater`. The requested source is installed on boot;
invalid values and failed installs stop the app. The verified source marker
and Python environment are stored in the app's persistent `/data` directory.
The generated environment is rebuilt whenever its app-image compatibility
changes. Startup verifies the real import path before reporting a source as
active.

See [DOCS.md](./openhop_repeater_main/DOCS.md) for complete installation,
configuration, hardware, update, storage, and troubleshooting instructions.

## Repository structure

```text
openhop_repeater_main/
├── config.yaml          # Home Assistant app metadata + branch_or_pr option
├── config.yaml.example  # openHop Repeater configuration template
├── Dockerfile
├── DOCS.md
├── README.md
├── CHANGELOG.md
├── run.sh
└── rootfs/              # app runtime helpers
```

## License

The Home Assistant app files in this repository are licensed under the MIT
License. openHop Repeater is distributed under its own upstream license.
