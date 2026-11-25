# Svago Motion Mastery 1.1.1

Fixes heat and massage timer choices changing saved Continuous preferences before
a waiting request can run. Requires Home Assistant 2026.10.0 or newer.

## Changes since 1.1.0

- Timer selections queued behind Home or another setting preserve saved
  Continuous preferences. Discarding a waiting request after Stop, failed Home,
  replacement, cancellation or reconnect leaves those preferences intact.
- A finite timer selection retires active Continuous renewal only when its
  validated command is ready to be sent. Cancelling it while queued preserves
  the existing renewal session.
- Newer timer choices still supersede older queued Continuous selections.
  An admitted Continuous choice can be armed while heat or massage is off
  without starting that function.

Direction pulses remain **0.5 seconds** for headrest, seat and footrest, and
**0.3 seconds** for backrest. The first wake after HA Home retains its separate
**0.25-second** interval and fresh-feedback requirement. Saved memories and
existing entity identities are preserved.

## Install or upgrade

Install the released version through Marketplace/HACS, adding
`roweboat123/svago-home-assistant` as a custom **Integration** repository if needed.
For a manual installation, replace `/config/custom_components/svago` with the
archive's `custom_components/svago` folder. Restart Home Assistant and wait for
**Connection status → Ready**.

Keep the existing integration entry and Home Assistant storage to preserve saved
memories and timer preferences. Fresh installations start with all three memory
slots empty. Existing automations are not changed automatically.

Automated validation passed **1,306 tests on each of Home Assistant 2026.10.0
and 2026.11.2**, plus Ruff lint and formatting. The timer regression tests use
simulated transport and feedback; they do not add physical chair or long-duration
renewal evidence. See the [validation record](https://github.com/roweboat123/svago-home-assistant/blob/1.1.1/VALIDATION.md)
and [automation guide](https://github.com/roweboat123/svago-home-assistant/blob/1.1.1/docs/AUTOMATIONS.md).
