# Backrest recovery check — 1.1.0rc10

The owner reported that rc9's 50 ms backrest taps produced no movement or error,
while the backrest slider still worked. This candidate restores the exact rc8
controller behavior: a **0.1-second normal backrest pulse** with bounded startup
waiting. It does not fix the previously excessive adjustment size or establish
a smaller physical step. The firmware cause remains unverified.

**Seat tilt up** and **Seat tilt down** remain under Controls with their original
entity identities and 0.25-second normal pulse. Home handling and wake timing are
unchanged. Version 1.0.2 remains the stable release.

## Install

1. Extract `svago-home-assistant-1.1.0rc10.zip` and replace
   `/config/custom_components/svago` with the archive's `custom_components/svago`
   folder. Keep the existing integration entry and Home Assistant storage.
2. Restart Home Assistant. Disconnect nRF Connect and keep the Bluetooth remote
   off so Home Assistant can connect.
3. Wait for **Connection status → Ready** and check that diagnostics report
   `1.1.0rc10`.

## Confirm backrest responds again

From a comfortable position away from the chosen direction's endpoint, press
**Backrest recline** or **Backrest upright** once. Observe whether it moves and
stops. A normal pulse is 0.1 seconds,
but without forward position feedback it can continue until the first forward
report or the two-second startup deadline. An observed endpoint is a quiet
no-op. Do not infer a fixed physical step from the nominal pulse duration.

After that single press finishes, download diagnostics immediately from the
Svago device/integration page, before sending another control. Report which
backrest button was pressed, whether it moved and stopped, and whether the
adjustment was still too large. One working press provides fresh evidence for
further timing investigation. If it still does not respond, capture that result
instead; a successful write alone does not prove movement.

Only this response check is requested. No repeated Home, seat or long
Continuous-timer tests are needed for this recovery candidate. Stop remains
available to interrupt movement. Repeated direction presses join one burst with
a three-second overall cap and cannot extend the two-second no-progress deadline.

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

The first foreground wake after HA Home retains the 0.25-second interval and
requires a fresh report afterward, with a 3.25-second default maximum wait.
Physical controller or remote Off does not itself arm the interval; startup
reliability through that path remains unresolved. **Wired controller (estimated)**
is an action-based estimate, not measured power feedback.

For automation recall, use `svago.recall_memory` after Ready; see
[automation guidance](AUTOMATIONS.md). Keep saved slots unchanged. The
[validation record](../VALIDATION.md) preserves earlier physical evidence and
remaining limits.
