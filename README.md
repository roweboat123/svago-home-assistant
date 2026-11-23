# Svago Motion Mastery for Home Assistant

Local Bluetooth control for Svago Motion Mastery chair.
Version **1.0.1**.

A compatible HA Bluetooth adapter or active Bluetooth proxy must be within range.

This is an independent community integration, not an official Svago product.
Support is based on the Motion Mastery protocol; compatibility with other Svago
controllers or firmware has not been established.

## What's new in 1.0.1

Improved automatic reconnection after the chair loses power. If required Bluetooth
services or characteristics are missing, the integration retries with fresh service
discovery once. Seamless reconnection after turning the chair's power off and back
on was confirmed on the development chair.

## Install and connect

Requires Home Assistant **2026.10.0 or newer**. Home Assistant 2026.11+ provides
Marketplace; 2026.10 installations can use HACS.

Add [`roweboat123/svago-home-assistant`](https://github.com/roweboat123/svago-home-assistant)
as a custom **Integration** repository in Marketplace/HACS, then download it and
restart Home Assistant. Catalog inclusion requires a separate review.

For a manual installation, download the ZIP from
[Releases](https://github.com/roweboat123/svago-home-assistant/releases):

1. Copy its `custom_components/svago` folder into your Home Assistant
   `/config/custom_components/` directory.
2. Restart Home Assistant.
3. Keep the chair powered on and turn off the **Motion Mastery Bluetooth remote**.
4. In **Settings → Devices & services**, open the discovered Svago entry, or
   choose **Add integration → Svago Motion Mastery** and enter your chair's
   Bluetooth address.
5. Wait for **Connection status → Ready**, then use the controls.

The official Bluetooth remote will occupy the chair's connection; turn it off before using
Home Assistant.

For an upgrade, replace the integration folder and restart; keep your existing
integration entry. Existing entity identities and saved memories are retained.
Fresh installations start with **Memory 1, Memory 2 and Memory 3 empty**, ready for
you to save your own settings. No personal chair address or someone else's
recorded memories are installed.

See [Publishing](docs/PUBLISHING.md) for the release and catalog submission process.

## Controls

| Control | What it does |
| --- | --- |
| Headrest, backrest, seat and footrest position | Move toward a reported position |
| Headrest, backrest and footrest direction buttons | Make a short adjustment |
| Lumbar inflate / deflate | Send a three-second adjustment, then release |
| Stop movement | Interrupt movement and discard older waiting adjustments |
| Heat | Turn master heat on or off |
| Backrest / seat / footrest heat | Choose Off, Low, Medium or High |
| Heat timer | 60, 120, 180 minutes or Continuous |
| Massage | Off, Gentle, Relax, Stretch, Wave or Vigorous |
| Massage timer | 15, 30, 45 minutes or Continuous |
| ZGR / Lay flat | Apply the captured native program |
| Memory 1–3 | Recall a saved Home Assistant profile, or save while Set memory is on |
| Set memory | Arm one memory save |
| Turn on | Wake the chair's controller without toggling it off |

**Turn on**, **Memory 1–3** and **Set memory** appear under **Configuration**.
Other controls appear under **Controls**. The diagnostic sensors appear under
**Diagnostics**.

Direction buttons stop automatically. Headrest and footrest use half-second
presses; backrest uses a quarter second. Repeating the same direction extends
one burst, capped at three seconds, instead of queueing future travel. Stop,
a reversal or a different adjustment takes over after releasing the old command.
A direction press at an observed limit quietly does nothing.

Position ranges are the chair's observed raw units, not percentages: headrest
0–17, backrest 0–22, seat 0–40 and footrest 0–28. The chair may move the seat while
reclining the backrest; returning the backrest does not necessarily return the seat.
Lumbar has no established position feedback, so it remains buttons.

**ZGR and Lay flat also select Relax massage and heat with their captured timer
and zone settings.** They are full chair programs, not movement-only buttons.
They apply finite timers and exit Continuous.

## Continuous heat and massage

Select **Continuous** in either timer, then turn Heat on or choose a massage mode.
Selecting Continuous while the function is already running starts managed renewal.
Selecting it while the function is off only remembers the choice.

| Function | Native duration | Renew after |
| --- | --- | --- |
| Heat | 180 minutes | 175 minutes |
| Massage | 45 minutes | 40 minutes |

Each renewal is one brief Bluetooth command. Home Assistant renews five minutes
before native expiry to allow time for a busy chair or delayed response. It does
not disable the chair's timer.

Use Heat Off or Massage Off to stop that function. The Continuous choice stays
selected for the next explicit start. Connection loss or a Home Assistant restart
ends the running renewal session but retains the choice. Reconnecting never
restarts heat, massage or a renewal session automatically.

**Heat continuous status** and **Massage continuous status** show whether HA is
actually managing renewal. Their details explain why a session is waiting or stopped.
They describe renewal, not proof that heating or massage physically stopped.

| Status | Meaning |
| --- | --- |
| Not selected | A finite timer is selected |
| Waiting for start | Continuous is remembered; explicitly start the function |
| Starting | Preparing Continuous renewal |
| Running | Home Assistant is managing renewal |
| Waiting for massage | Heat renewal is deferred while finite or unmanaged massage runs |
| Stopped | Renewal cannot continue; see the reason in the sensor's details |

Heat renewal waits during finite or unmanaged massage because the shared command
could restart that massage's timer. Both can renew when both have active HA
Continuous sessions. Long deferral can allow heat's native timer to expire; HA
leaves it off. Missing feedback or a failed renewal ends the affected session,
and any still-running function remains subject to its last native timer.

Finite timer selectors display the configured duration, not a live countdown.
The longer renewal combinations and simultaneous heat/massage operation still
need physical validation beyond the separately demonstrated renewal behavior.

## Save and recall memories

These are independent Home Assistant profiles; they do not edit or synchronize
with the Bluetooth remote's memory slots.

1. Adjust the chair and choose its heat, massage and timer settings.
2. Let the chair settle, then turn on **Set memory**.
3. Press **Memory 1**, **Memory 2** or **Memory 3** to save that slot.
4. Set memory turns off automatically. With it off, press a Memory button to recall.

While Set memory is on, other user controls are disabled. Turn it off to cancel.
Saving requires fresh position feedback stable for two seconds and sends no chair
command. Controls unlock after the save completes or fails.

Profiles include positions, heat state/levels, massage mode, finite durations and
each function's Continuous choice. Recalling a profile restores those choices;
an off function remains off, with Continuous remembered for a later explicit start.
Active functions saved as Continuous begin managed renewal after successful recall.
Old profiles without the new choices retain their finite timers until saved again.

A newer memory selection replaces an older request; obsolete recalls do not queue
to run later. The chair may need time to stop and acknowledge the new target.
Stored profiles survive restarts and integration reloads. Deleting the integration
entry also deletes its stored profiles and timer preferences.

## Connection and troubleshooting

**Connection status → Ready** requires a live Bluetooth connection, notifications,
fresh usable chair status and capacity for the full command. It does not prove
that a motor moved. Controls become unavailable when this path is not ready.

- **After power returns:** the integration reconnects automatically. Keep the Motion
  Mastery Bluetooth remote off and wait for **Ready** before using the controls.
  Reconnection does not replay movement or restart heat, massage or Continuous sessions.
- **Cannot connect:** confirm power, turn off the Motion Mastery Bluetooth remote,
  and check that HA has a connectable adapter or active Bluetooth proxy in range.
- **Request completed but no movement:** check the requested direction and position;
  a completed command does not prove physical travel. Download diagnostics if it repeats.
- **Last problem:** retained history of the latest failed or blocked action. Check
  its time; an old problem does not mean the current connection is broken.
- **Continuous is selected but inactive:** read its status sensor, restore the
  connection if needed, then explicitly start the function again.
- **A memory button is unavailable:** an empty slot becomes available as a save
  destination when Set memory is on.

Download diagnostics from the Svago device/integration page for connection details,
recent actions, position feedback and timer-renewal outcomes. This sends no command.
Review any logs before attaching them to a public issue.

## Optional startup automation

If an automation issues a chair command, it can first wait for **Ready**.
A generic example is provided in [Automation examples](docs/AUTOMATIONS.md).

## Development and license

See [Contributing](CONTRIBUTING.md), [Validation](VALIDATION.md),
[Release notes](CHANGELOG.md) and [Publishing](docs/PUBLISHING.md).
The Python software is MIT licensed. Third-party names and brand artwork remain
the property of their owners; see [NOTICE](NOTICE).
