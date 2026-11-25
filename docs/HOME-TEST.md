# Home and startup candidate checks — 1.1.0rc8

The chair's exact native Home command was captured from Bluetooth remote
power-off and successfully replayed from a phone without disconnecting. The owner
then confirmed that **Return to neutral** worked in Home Assistant with 1.1.0rc1.
Version 1.1.0rc8 retains that button under **Configuration**, with the same entity
identity and native command. It now finishes Home before running the latest
requested movement, recall, preset or comfort/settings action. Stop remains
immediate and clears any waiting action. The retained quarter-second pause was
observed in rc6 diagnostics; reliable first-press recall still needs confirmation.
Version 1.0.2 remains the stable release.

This candidate shortens the normal backrest pulse to 0.1 seconds. Without
forward feedback it can still continue until the first forward report or the
two-second startup deadline, so it does not promise a fixed physical increment.
The owner could raise the footrest with the wired controller in the failed HA
state, and HA raise worked after Home completed fully. The new Home handoff is
a candidate based on these observations; its physical effectiveness and the
firmware reason remain unverified. Footrest pulse and wake timing are unchanged.

## Install

1. Extract `svago-home-assistant-1.1.0rc8.zip` and replace
   `/config/custom_components/svago` with the archive's `custom_components/svago`
   folder. Keep the existing integration entry and Home Assistant storage.
2. Restart Home Assistant. Disconnect nRF Connect and keep the Bluetooth remote
   off so Home Assistant can connect.
3. Wait for **Connection status → Ready**. Check that diagnostics report
   `1.1.0rc8` and that **Return to neutral** appears under **Configuration**.

The default-enabled **Wired controller (estimated)** sensor appears under
**Diagnostics**. It starts Unknown after a restart or reconnect, estimates On
after a successful HA wake, and estimates Off after a successful HA Return to
neutral. It shows Unknown when not ready, with lost or unusable status, and while
wake/Home is in progress or incomplete. Physical controls or the Bluetooth
remote can make the estimate wrong; it is not measured power feedback and does
not change control behavior.

The standalone Turn on button is removed. Movement buttons and sliders, lumbar,
preset, memory-recall, Heat On and non-Off massage actions activate the controller
automatically. Reliable first-press recall still needs confirmation; Home's
native command and completion checks are unchanged.

For the internal automation check, use `svago.recall_memory` targeting exactly
one saved Memory button after Ready. Existing automations are not changed
automatically. The action cannot save and does not update the button's
last-pressed timestamp; see [automation guidance](AUTOMATIONS.md).

## Check Home and the following control

1. From a comfortable position away from neutral, use **Return to neutral**.
   While it is travelling, choose **Footrest raise** once. Home should complete
   first, then the waiting footrest adjustment should run. You do not need to
   wait manually before choosing the next control.
2. Start away from neutral again and repeat with **Memory 3**, using its existing
   saved position away from neutral.
   Observe whether it waits for Home, then wakes the controller and recalls the
   saved position from that one press. This candidate waits for a fresh report
   after a 0.25-second pause. With normal feedback cadence, the wait may take
   about 0.3 seconds; its
   default maximum is 3.25 seconds after the wake acknowledgment. The saved
   target is sent once.
3. Start away from neutral again. While another Home is travelling, choose two
   different follow-up controls. Only the last choice should run after Home
   completes. Leave saved slots unchanged; a saved neutral memory does not test
   travel away from Home.
4. Optionally, from a position with room to move, press a backrest direction
   button once. Check the adjustment and that it stops; rc8's normal pulse is
   0.1 seconds, but it does not set a fixed physical step. If no forward position
   report arrives during the normal pulse, directional startup waiting allows up to
   two seconds; repeating presses cannot extend that no-progress deadline.

Before rc3, the owner confirmed in a direct Home-to-Memory-3 check that the first
press only woke the wired controller and the second recalled the position.
Waiting after Home did not prevent that behavior. The owner then confirmed rc3
works. The settling period starts after the first press's wake acknowledgment.
Later reports describe intermittent recall despite the wired controller waking.
Earlier rc6 diagnostics record Memory 1 after Home with an approximately
0.301-second settling interval. The owner is revising the Memory 3/Memory 1 toggle
automation as a possible contributor; its role is unconfirmed.
The new Home handoff still needs on-chair verification. Repeating Home keeps the
current Home and clears a waiting choice; Home failure or connection loss also
discards the waiting action. It is not replayed after reconnecting.
The quarter-second interval is armed after HA's Home command; physical controller
or remote Off does not itself arm it. That startup path remains unresolved.
Reliable first-press recall still needs confirmation. If a first
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
   travel after choosing a follow-up. Confirm that Home stops and the waiting
   control never runs. A new explicit control is allowed afterward; reliability
   after interrupted Home remains unverified.
4. Check one Home return with heat and massage running, including Continuous if
   normally used. Both functions should stop, with Continuous choices retained
   and renewal inactive. Nothing should resume without an explicit start.

The native invocation includes timer resets and heat-zone selections
backrest Low, seat Off, footrest Low. It does not save or replace Home Assistant
memory slots. A completed request confirms reported motor positions and comfort
function state; controller power and lumbar pressure are not independently
available as feedback. The estimated sensor does not establish either measurement.

If any result differs, download Svago diagnostics immediately and describe the
physical result. `last_home_request` records the command, feedback and outcome.
The command is not automatically resent on failure or reconnect.

The phone test started with heat and massage already Off and positions
`0/0/4/6`. It reached `0/0/0/0` after about 6.75 seconds, with notifications
continuing afterward. The later successful HA test was reported without separate
results for Stop during travel, active comfort shutoff or wired-controller
shutdown through the button.
