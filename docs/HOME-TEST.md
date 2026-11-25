# Native Home notes and optional checks — 1.1.0rc2

The chair's exact native Home command was captured from Bluetooth remote
power-off and successfully replayed from a phone without disconnecting. The owner
then confirmed that **Return to neutral** worked in Home Assistant with 1.1.0rc1.
Version 1.1.0rc2 moves that button to **Configuration**, with the same entity
identity and behavior. Version 1.0.2 remains the stable release.

## Install

1. Extract `svago-home-assistant-1.1.0rc2.zip` and replace
   `/config/custom_components/svago` with the archive's `custom_components/svago`
   folder. Keep the existing integration entry and Home Assistant storage.
2. Restart Home Assistant. Disconnect nRF Connect and keep the Bluetooth remote
   off so Home Assistant can connect.
3. Wait for **Connection status → Ready**. Check that diagnostics report
   `1.1.0rc2` and that **Return to neutral** appears under **Configuration**.

## Optional checks for additional evidence

The category change does not require another physical test. The original checks
below remain available if more detailed behavior needs to be recorded.

1. With heat and massage Off, move the chair partway out of neutral using the
   normal controls, then let it stop.
2. Press **Return to neutral** once. Observe whether the chair returns home and
   whether the wired controller switches off. Home Assistant should remain
   connected. The request finishes after two seconds of fresh neutral feedback.
3. From another comfortable posture, repeat and press **Stop movement** during
   travel. Confirm that it stops, then use normal controls again.
4. Check one Home return with heat and massage running, including Continuous if
   normally used. Both functions should stop, with Continuous choices retained
   and renewal inactive. Nothing should resume without an explicit start.

The native invocation includes timer resets and heat-zone selections
backrest Low, seat Off, footrest Low. It does not save or replace Home Assistant
memory slots. A completed request confirms reported motor positions and comfort
function state; controller power and lumbar pressure are not independently
available as feedback.

If any result differs, download Svago diagnostics immediately and describe the
physical result. `last_home_request` records the command, feedback and outcome.
The command is not automatically resent on failure or reconnect.

The phone test started with heat and massage already Off and positions
`0/0/4/6`. It reached `0/0/0/0` after about 6.75 seconds, with notifications
continuing afterward. The later successful HA test was reported without separate
results for Stop during travel, active comfort shutoff or wired-controller
shutdown through the button.
