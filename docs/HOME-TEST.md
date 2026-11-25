# Adjustment and Home checks — 1.1.0rc9

This candidate requests a **0.05-second backrest tap**, half the previous nominal
duration, and releases without waiting for the first position-progress report.
It also restores **Seat tilt up** and **Seat tilt down** under Controls, with their
original entity identities and the unchanged 0.25-second normal pulse. Neither
timing specifies a fixed physical adjustment or guarantees half the movement.
Version 1.0.2 remains the stable release.

The owner has confirmed these rc8 checks on the development chair:

- Footrest raise chosen during Home waits until Home completes, then runs.
- Memory 3 chosen once during Home waits and recalls the saved position.
- Choosing Headrest followed by Footrest during Home runs only the Footrest choice.
- Stop during Home cancels a waiting Memory 3.
- A fresh Footrest raise works after that Stop without another Home.

These passed checks do not need repeating for this candidate. They do not
establish every control, startup path or chair firmware. Home's command,
completion checks, latest-choice handling and wake interval are unchanged.

## Install

1. Extract `svago-home-assistant-1.1.0rc9.zip` and replace
   `/config/custom_components/svago` with the archive's `custom_components/svago`
   folder. Keep the existing integration entry and Home Assistant storage.
2. Restart Home Assistant. Disconnect nRF Connect and keep the Bluetooth remote
   off so Home Assistant can connect.
3. Wait for **Connection status → Ready**. Check that diagnostics report
   `1.1.0rc9`, that both seat buttons appear under **Controls**, and that
   **Return to neutral** remains under **Configuration**.

## Check the new adjustments

1. From a comfortable position with room to move, press **Backrest recline** once.
   Observe the adjustment and that it stops. Once settled, try **Backrest upright**
   once. The requested tap is 0.05 seconds; it does not extend to wait for the
   first forward position report. A very short press may produce little or no
   visible movement, and physical step size depends on the chair's response.
2. Press **Seat tilt up** once and let it finish, then try **Seat tilt down** once.
   Observe each direction and that it stops. Seat's normal pulse remains
   0.25 seconds. Without forward feedback it can continue until the first forward
   report or the two-second startup deadline. Headrest and footrest retain the
   same startup handling with their normal half-second pulses.

An observed endpoint is a quiet no-op. Repeated presses join one burst with a
three-second overall cap. They cannot extend the other axes' two-second
no-progress startup deadline. Stop remains available to interrupt movement.
No long Continuous-timer run is needed for these checks.

## Retained Home behavior

Movement, recall, preset and comfort/settings requests selected during Home wait
for completion, and only the latest choice runs. Repeated Home joins the current
Home without resending it and clears a waiting choice. Home failure, timeout,
stale feedback, disconnect or unload discard waiting actions. They are not
replayed after reconnecting. Stop immediately interrupts Home and drops the
waiting action; a new explicit control remains allowed afterward.

The first foreground wake after HA Home retains its 0.25-second settling interval
and requires a fresh report afterward. Normal feedback cadence may make this
about 0.3 seconds; the default maximum is 3.25 seconds after the wake acknowledgment.
The wake and saved target are each sent once. Physical controller or remote Off
does not itself arm this interval; startup reliability through that path remains
unresolved. The cause of the earlier intermittent memory report, including the
role of the owner's toggle automation, remains unconfirmed.

For automation recall, use `svago.recall_memory` targeting exactly one saved
Memory button after Ready. Existing automations are not changed automatically.
The action cannot save and does not update the button's last-pressed timestamp;
see [automation guidance](AUTOMATIONS.md). Ordinary Memory buttons retain their
save/recall behavior, so leave **Set memory** Off when recalling a saved position.
Keep the existing saved slots; these checks do not require overwriting them.

## Optional additional observations

These cover behavior beyond the successful rc8 checks, if more evidence is wanted:

- During Home from a comfortable nonneutral position, choose a follow-up, then
  press **Return to neutral** again. The original Home should continue without a
  second Home command, and the waiting follow-up should be cleared.
- Observe Home with heat and massage running, including Continuous if normally
  used. Both functions should stop, with Continuous choices retained and renewal
  inactive. Nothing should resume without an explicit start. A comfort/settings
  action chosen during Home should wait until Home completes.

Home finishes after two seconds of fresh neutral feedback with heat and massage
Off. It does not save or replace memory slots. Controller power and lumbar
pressure are not independently available as feedback. **Wired controller
(estimated)** remains an action-based estimate; physical controls or the remote
can make it wrong. See [validation details](../VALIDATION.md) for the historical
phone replay and the limits of the physical evidence.

If any result differs, download Svago diagnostics immediately and describe what
moved, which control was selected, and whether the wired controller woke.
`last_home_request` records Home's command, feedback and outcome. Home is not
automatically resent on failure or reconnect.
