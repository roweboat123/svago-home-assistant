# Home and startup candidate checks — 1.1.0rc3

The chair's exact native Home command was captured from Bluetooth remote
power-off and successfully replayed from a phone without disconnecting. The owner
then confirmed that **Return to neutral** worked in Home Assistant with 1.1.0rc1.
Version 1.1.0rc3 places that button under **Configuration**, with the same entity
identity and behavior. Version 1.0.2 remains the stable release.

## Install

1. Extract `svago-home-assistant-1.1.0rc3.zip` and replace
   `/config/custom_components/svago` with the archive's `custom_components/svago`
   folder. Keep the existing integration entry and Home Assistant storage.
2. Restart Home Assistant. Disconnect nRF Connect and keep the Bluetooth remote
   off so Home Assistant can connect.
3. Wait for **Connection status → Ready**. Check that diagnostics report
   `1.1.0rc3` and that **Return to neutral** appears under **Configuration**.

The standalone Turn on button is removed. Movement buttons and sliders, lumbar,
preset, memory-recall, Heat On and non-Off massage actions activate the controller
automatically. The rc3 startup changes need an on-chair check; Home's native
command and completion checks are unchanged.

## Check the rc3 startup changes

1. Use **Return to neutral** and let it finish.
2. Press **Memory 3** once, using its existing saved position away from neutral.
   Observe whether that first press wakes the controller and moves the chair to
   the saved position. This candidate waits for fresh feedback after a two-second
   settling interval, with a five-second maximum after the wake acknowledgment;
   it sends the saved target once.
3. Optionally repeat Home followed by one press of another memory that already
   contains a position away from neutral. Memory 1's saved neutral pose does not
   test travel away from Home. Leave the saved slots unchanged.
4. From a position with room to move, press an affected direction button once.
   Check that it makes a short adjustment and stops. If no forward position
   report arrives during the normal pulse, rc3 allows startup waiting for up to
   two seconds; repeating presses cannot extend that no-progress deadline.

The owner confirmed in a direct Home-to-Memory-3 check that the first press only
wakes the wired controller and the second recalls the position. Waiting after
Home did not prevent the reported behavior. The candidate's settling period
starts after that first press's wake acknowledgment.
These changes also address the diagnosed footrest tap pattern. The physical
cause and effectiveness of these changes are still unconfirmed. If a first
press fails or movement differs from the expected result, download diagnostics
immediately and describe what moved and whether the controller woke. No long
Continuous-timer run is needed here.

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
