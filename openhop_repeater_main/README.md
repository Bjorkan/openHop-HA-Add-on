# Home Assistant App: openHop Repeater

Run openHop Repeater as a Home Assistant app with:

- a complete YAML configuration file;
- branch or pull-request selection from the app Configuration tab;
- a startup check that the configured repeater and core refs are the newest
  upstream versions, updating before start when a branch or PR has moved;
- persistent source and runtime data;
- verification that the requested source is the package Python actually imports;
- refusal to start when the requested source cannot be installed and verified;
- support for SPI/GPIO radios, USB devices, serial KISS modems, TCP modems, and
  companion services.

The configuration file is created on first start. Later image updates merge
missing packaged defaults without replacing user values. The file is available
on the Home Assistant host at:

```text
/addon_configs/*_openhop_repeater_main/config.yaml
```

See [DOCS.md](DOCS.md) for installation and configuration instructions.
