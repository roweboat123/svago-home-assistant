# Direction-pulse check — 1.1.0rc13

Isolated direction presses now use these fixed command durations without extending
them to wait for a reported position increment:

| Axis | Pulse |
| --- | --- |
| Headrest | 0.3 seconds |
| Backrest | 0.3 seconds |
| Seat | 0.5 seconds |
| Footrest | 0.3 seconds |

The earlier 0.25-second backrest pulse was confirmed suitable, and the owner found
rc12's quarter-second seat pulse too short. The new durations still need chair
confirmation. They do not define a fixed physical step. Entity identities,
Home behavior and the separate 0.25-second wake interval are unchanged.
Version 1.0.2 remains stable.

## Install

1. Extract `svago-home-assistant-1.1.0rc13.zip` and replace
   `/config/custom_components/svago` with the archive's `custom_components/svago`
   folder. Keep the existing integration entry and Home Assistant storage.
2. Restart Home Assistant. Disconnect nRF Connect and keep the Bluetooth remote
   off so Home Assistant can connect.
3. Wait for **Connection status → Ready** and check that diagnostics report
   `1.1.0rc13`.

## Observe isolated direction presses

From a comfortable position away from the chosen direction's endpoint, press a
direction button once. Observe whether it moves and stops, and whether the
adjustment is suitable. Let that press finish before choosing another direction
or axis. The pulse specifies a command duration, not an angle or position
increment. An observed endpoint remains a quiet no-op.

Stop, reversal and replacement controls retain their interruption behavior.
Repeated presses in the same direction join one burst with a three-second cap.
The successful Home checks do not need repeating; long Continuous-timer checks
are outside this candidate's scope.

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
