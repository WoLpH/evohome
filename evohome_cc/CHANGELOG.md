# Changelog for Honeywell evohome custom component

This is a Home Assistant `custom_component` that supports Honeywell evohome multi-zone heating systems (EU-only).

## 0.9.x - WIP

0.9.6 Fix bug with _next_switchpoint_time in set_operation_mode() (PR 31).

0.9.5 Remove some logspam.  Change name of TCS for compliance with HA slugify rules.

0.9.2 Initial support for [custom_updater](https://github.com/custom-components/custom_updater/wiki/Installation) platform.  Many improvements to exception handling (ignoring v1 api exceptions, warnings for ConnectionErrors).

