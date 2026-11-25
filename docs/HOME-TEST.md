# Quarter-second seat check — 1.1.0rc12

**Seat tilt up** and **Seat tilt down** now use a **fixed 0.25-second pulse for an
isolated press**, without extending it to wait for a reported position increment.
This matches the backrest mode the owner confirmed suitable in rc11. Seat's new
timed response still needs confirmation; pulse duration does not define a fixed
physical step.

The seat buttons keep their original entity identities and directions. Headrest
and footrest retain half-second normal pulses and startup waiting until the first
forward report or the two-second deadline if needed. Home handling and its separate
0.25-second wake interval are unchanged. Version 1.0.2 remains stable.

## Install

1. Extract `svago-home-assistant-1.1.0rc12.zip` and replace
   `/config/custom_components/svago` with the archive's `custom_components/svago`
   folder. Keep the existing integration entry and Home Assistant storage.
2. Restart Home Assistant. Disconnect nRF Connect and keep the Bluetooth remote
   off so Home Assistant can connect.
3. Wait for **Connection status → Ready** and check that diagnostics report
   `1.1.0rc12`.

## Observe a seat press

From a comfortable position away from the chosen direction's endpoint, press
**Seat tilt up** or **Seat tilt down** once. Observe whether it moves and stops,
and whether the adjustment is suitable. Let it finish before choosing the other
direction. This is a timed quarter-second pulse, not a request for a particular
angle or position increment. An observed endpoint remains a quiet no-op.

Stop, reversal and replacement controls retain their interruption behavior.
Repeated presses in the same direction join one burst with a three-second cap.
Backrest and the successful Home checks do not need repeating; long
Continuous-timer checks are outside this candidate's scope.

Sliders still target whole reported positions. Rescaling their numbers would not
add precision; this build leaves slider range and step unchanged.

## Retained Home evidence and behavior

The owner confirmed these rc8 cases on the development chair:

- Footrest raise chosen during Home waits until Home completes, then runs.
- Memory 3 chosen once during Home waits and recalls the saved position.
- Choosing Headrest followed by Footrest during Home runs only the Footrest choice.
- Stop during Home cancels a waiting Memory 3.
- A fresh Footrest raise works after that Stop without another Home.

These checks do not need repeating. They do not establish every control,
startup path or chair firmware. Repeated Home joins the current operation and
clears a waiting choice. Stop interrupts Home and discards the waiting action.
Home failure, timeout, stale feedback, disconnect or unload discard waiting
actions without replaying them later.

The first foreground wake after HA Home retains the separate 0.25-second interval
and requires a fresh report afterward, with a 3.25-second default maximum wait.
Physical controller or remote Off does not itself arm the interval; startup
reliability through that path remains unresolved. **Wired controller (estimated)**
is an action-based estimate, not measured power feedback.

For automation recall, use `svago.recall_memory` after Ready; see
[automation guidance](AUTOMATIONS.md). Keep saved slots unchanged. The
[validation record](../VALIDATION.md) preserves earlier physical evidence and
remaining limits.
