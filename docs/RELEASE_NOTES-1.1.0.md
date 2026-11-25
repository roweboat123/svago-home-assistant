# Svago Motion Mastery 1.1.0

Adds native Return to neutral, orderly control handoffs after Home, recall-only
automations and an estimated controller-state sensor. Requires Home Assistant
2026.10.0 or newer.

## Changes since 1.0.2

- **Return to neutral** runs native Home from Configuration, preserving memory
  slots and Continuous choices while stopping active renewal.
- Home finishes before the latest waiting control runs. **Stop movement** interrupts
  immediately and clears waiting actions; failures and disconnects discard them.
- Actions that need the controller awake activate it automatically. The first wake
  after Home includes a 0.25-second interval with fresh feedback before the next
  command. The standalone Turn on button is removed.
- **Wired controller (estimated)** shows On, Off or Unknown based on HA actions.
  It is an estimate, not measured controller power.
- `svago.recall_memory` provides recall-only automation targeting for one Memory
  button. Ordinary Memory buttons retain their save/recall behavior; existing
  automations are not changed automatically.
- Preset failures before dispatch preserve Continuous sessions. Uncertain final
  slider release retains the required stop before the next control.
- **Seat tilt up/down** retain their original identities. Fixed direction pulses
  are 0.5 seconds for headrest, seat and footrest, and 0.3 seconds for backrest,
  without extending for a reported position increment. Joined presses retain a
  three-second cap.

## Install or upgrade

Install the released version through Marketplace/HACS, adding
`roweboat123/svago-home-assistant` as a custom **Integration** repository if needed.
For a manual installation, replace `/config/custom_components/svago` with the
archive's `custom_components/svago` folder. Restart Home Assistant and wait for
**Connection status → Ready**.

Keep the existing integration entry and Home Assistant storage: they contain
saved memories and timer preferences. Do not delete and recreate the entry.
Fresh installations start with all three memory slots empty.

Local automated validation passed **1,280 tests on each of Home Assistant
2026.10.0 and 2026.11.2**, plus Ruff lint and formatting checks.

The owner confirmed rc14's headrest/footrest adjustment and the recorded Home
handoff checks on the development chair. This does not establish a fixed physical
step, compatibility with every chair/firmware, or all longer Continuous timer
combinations. See the [validation record](https://github.com/roweboat123/svago-home-assistant/blob/1.1.0/VALIDATION.md)
and [automation guide](https://github.com/roweboat123/svago-home-assistant/blob/1.1.0/docs/AUTOMATIONS.md).
