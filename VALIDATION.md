# Validation — Svago 1.1.1

## Scope

Prepared November 25, 2026. The integration was developed against a Motion Mastery
chair using captured Bluetooth traffic and physical command tests. Automated
checks exercise Home Assistant behavior with simulated transport and feedback;
they do not establish behavior on other chair models, firmware or adapters.

Version 1.1.1 passed on both Home Assistant **2026.10.0** and **2026.11.2**:
**1,306 tests** on each, plus Ruff lint and formatting.
Five dependency deprecation warnings remain on each version. Checks cover fixed
pulses in both directions, cadence, joined-tap caps, feedback and interruption
safeguards. Direction-pulse timing, Home movement and the post-Home wake interval
retain their 1.1.0 behavior.
Existing checks cover Home completion with only the latest follow-up, Stop and
failure handling, delayed memory lookup and settings-lock races,
recall-only targeting, failures before preset writes,
uncertain final slider-stop delivery,
the estimated controller state, its diagnostic sensor and export, uncertainty
after interrupted feedback or commands, and passive feedback without power
inference. It also covers fresh feedback after the post-Home wake interval,
interruption and reconnect handling, removal of the standalone Turn on entity,
and the readiness-only automation example with its guarded caller. Existing
tests retain the exact native Home frame, captured nonmonotonic travel, stable
neutral detection, Continuous sessions, memory-data preservation, fresh comfort
acknowledgments and elapsed lumbar deadlines. The preceding rc1 baseline passed
1,033 tests on each Home Assistant version. Reproduce these checks with
[CONTRIBUTING.md](CONTRIBUTING.md).

The 1.1.1 timer fix adds 26 regression cases. Twenty cover heat/massage finite
and Continuous selections discarded while waiting for Home, including Stop,
failed Home, replacement, reconnect and cancellation. Four confirm that a timer
choice changes its saved preference once after verified Home completion. Two
cancel finite timer selections waiting behind the settings lock and verify that
the saved choice and existing renewal worker remain intact. Existing tests cover
newer finite choices superseding queued Continuous selections.

Continuous choices now change saved state only after operation admission. A
finite choice clears Continuous and retires its worker only after packet
validation, immediately before dispatch. Delivery may be uncertain if the write
then fails. Selecting Continuous while a function is off still arms a future
explicit start without turning it on. These checks use simulated transport and
feedback; they add no new physical chair or long-duration renewal evidence.

The exact native Home packet was captured from the
remote and then replayed independently from a phone. The owner confirmed that
the chair returned home to neutral, with Bluetooth status notifications
continuing afterward. The owner subsequently confirmed that the Home Assistant
button worked in 1.1.0rc1.

Version 1.1.0 was the stable release of the owner-confirmed rc14 behavior, with
no behavioral changes. It included Return to neutral under Configuration and
removed the standalone Turn on button. Version 1.1.1 retains Home's identity,
native command and completion checks from rc14.

This release retains rc7's recall-only action for a single Memory button, preserves
Continuous sessions when a preset fails before its write, and retains the stop
requirement when a slider's final release delivery is uncertain. The new action
does not enter the save path, and existing automations are not converted to it.
The manual archive includes its YAML action metadata.

The owner reported that rc9's 50 ms backrest taps produced no physical movement
or error while the backrest slider worked. Version rc10 restored rc8's controller
behavior, then rc11 introduced a fixed 0.25-second backrest pulse without waiting
for a reported position increment. The owner confirmed that rc11's backrest
button press was suitable. This confirms the reported response on the development
chair, not an exact physical step size or behavior on other hardware.

The owner found rc12's fixed quarter-second seat pulse too short. Version rc13
switched every axis to fixed timed pulses. Version rc14 increases headrest and
footrest from 0.3 to 0.5 seconds, retaining backrest at 0.3 seconds and seat at
0.5 seconds. None extends to wait for a reported position increment. The new
headrest and footrest response was subsequently confirmed suitable by the owner
with rc14. That report validates the requested headrest/footrest adjustment on
the development chair, without separately establishing every axis or firmware.
The earlier 0.25-second backrest approval does not establish the changed duration. The
three-second burst cap, entity identities and direction mapping remain. Earlier
quarter-second headrest and footrest presses were reported unresponsive. The
firmware cause of the failed 50 ms backrest taps remains unverified, and
physical-step calibration is not established.

The latest rc7 footrest report contains four no-progress errors across three
command bursts and one joined press. Writes returned successfully, feedback
stayed fresh but unchanged, and there were no disconnects. The owner confirms
that the footrest physically stayed still and that the HA footrest slider also
fails. The latest Home request was interrupted by Headrest inward. In the failed
HA state, the owner could raise the footrest with the wired controller. After
Return to neutral completed fully, HA Footrest raise worked again. These
observations focus the investigation on the handoff after interrupted Home;
the exact firmware reason is unverified. The owner subsequently confirmed five
rc8 cases: queued Footrest raise after Home, one-press Memory 3 recall selected
during Home, only the latest Headrest-then-Footrest choice running, Stop cancelling
a waiting Memory 3, and a fresh Footrest raise working after Stop without another
Home. These results cover those reported cases on the development chair.

While native Home is active, foreground movement, recall, preset and
comfort/settings requests wait for it to complete. Only the latest request is
retained. Repeated Home joins the current operation and clears any waiting
follow-up without resending Home. Stop immediately cancels Home and the waiting
action. Home failure, timeout, stale feedback, disconnect or unload discard
waiting controls without sending their wake or target. Cancelling a waiting
caller leaves Home running and does not revive an older request. A new explicit
control after Stop remains allowed; no Home or queued control is replayed
automatically. Fresh Footrest raise after a deliberate mid-Home Stop was confirmed;
recovery for other controls through that path has not been established. Set memory
still cannot be armed while the chair is busy.

The new **Wired controller (estimated)** diagnostic sensor is enabled by default.
It estimates On after a successful HA wake and Off after successful HA Return to
neutral completion. It starts Unknown and shows Unknown when the connection is
not ready, after restart/reconnect or lost/unusable status, and while wake/Home
is in progress or incomplete. The estimate is not persisted and does not gate
actions, skip activation or change packets or timing. No actual power feedback
has been decoded; physical controls and the
Bluetooth remote can make this action-based estimate wrong without detectable
feedback.

The directional-startup handling introduced in rc3 addressed an earlier capture
where a half-second footrest press showed no forward feedback while a slider moved using
the same direction commands. This supports a startup-timing hypothesis without
establishing the cause. That change bounded startup waiting to two seconds and
kept the three-second absolute burst cap. The owner subsequently confirmed that
1.1.0rc3 works; the physical cause remains unproven. Version rc10 restored
backrest's startup allowance after the unsuccessful rc9 timed-tap experiment.
Version rc11 introduced a fixed quarter-second backrest pulse, confirmed suitable
by the owner. Version rc12 extended timed mode to seat, and rc13 to every axis.
Version rc14 uses 0.5 seconds for headrest, seat and footrest, and 0.3 seconds for
backrest. The earlier two-second no-progress extension no longer applies to
direction buttons.

Before rc3, the owner reported that the first memory press after Home only woke
the wired controller and the second performed recall, regardless of time spent waiting
after Home. One diagnostic capture contains a neutral Memory 1 recall followed
by a successful nonneutral Memory 3 recall, so it does not capture that failure.
In a separate controlled check, the owner went directly from Home to Memory 3
and confirmed that the first press only woke the controller; the second moved it.
The integration tracks a Home write attempt, then waits for a fresh report after
a 0.25-second settling interval following the next foreground wake acknowledgment.
With normal feedback cadence this wait may take about 0.3 seconds; the existing
three-second stale-feedback limit gives a 3.25-second default total bound on this wait.
The wake command and dependent memory target each remain single sends. Normal
actions without a preceding Home retain their timing; Home, reconnects and passive
status never start a wake on their own. The owner initially reported that rc5
worked as intended, then reported intermittent Home-to-Memory-3 recall: the wired
controller wakes, but recall occasionally does not start. Earlier rc6 diagnostics
recorded Memory 1 after Home,
with a post-wake settling interval of approximately 0.301 seconds. The owner is
revising the Memory 3/Memory 1 toggle automation as a possible contributor.
Its role is unconfirmed. The later rc8 check confirmed one-press Memory 3 recall
selected during Home; this does not establish every automation or startup path.

The owner previously reported completing internal testing of the fixes supplied
for stable 1.0.2. Version 1.1.0 retains those fixes.

The owner confirmed seamless reconnection on the affected chair with `1.0.1rc1`
after reporting the missing-FFF0 setup error following power loss. The connection
code from 1.0.1 is retained in 1.1.0. This validates recovery on that installation;
incomplete cached services remain an inferred cause, not a captured backend
diagnosis. See [the power-cycle recovery check](docs/RECONNECT-TEST.md).

GitHub-hosted hassfest and Marketplace/HACS validation run on each push. Their
results are available under [Actions](https://github.com/roweboat123/svago-home-assistant/actions).
A release and catalog submission require successful validation for the published
source revision. Archive integrity, packaged bytes, documentation links and
empty-memory setup are also checked during release preparation.

## Physical evidence

- The native remote-Off/Home packet returned the chair from reported positions
  0/0/4/6 to all zero in approximately 6.75 seconds during independent phone
  replay. The log contains 329 valid status frames and continued notifications
  after arrival, without a disconnect. Brief movement away from zero occurred
  during the sequence and is allowed by the Home monitor.
- The owner reported a successful Home Assistant button test with 1.1.0rc1.
  The report did not separately describe Stop during travel, active heat/massage
  shutoff or wired-controller shutdown through that button.
- With rc8, the owner confirmed queued Footrest raise and one-press Memory 3
  recall during Home, replacement of Headrest by the later Footrest choice, and
  Stop cancelling a waiting Memory 3. A fresh Footrest raise also worked after
  Stop without another Home. Repeated Home, other post-Stop controls and physical
  controller/remote-Off startup are not established by these checks.
- Heat and massage were already Off during the phone replay. The owner confirmed
  that holding the side dial and switching the remote Off also return home and
  turn the wired controller Off; controller power is not independently observable
  in the captured status fields.
- The owner reported no backrest movement or error from rc9's 50 ms taps while
  the backrest slider worked. This establishes the unsuccessful candidate result,
  not its firmware cause. The owner subsequently confirmed that rc11's fixed
  quarter-second backrest press was suitable. The owner found rc12's quarter-second
  seat pulse too short. No later approval of the changed backrest or seat durations
  is separately recorded here. The owner confirmed rc14's headrest and footrest
  adjustment was suitable. Version 1.1.0 retains that behavior; a fixed physical
  step or compatibility with all hardware is not established.
- The owner confirmed seamless reconnection with the power-cycle recovery fix.
  No manual integration reload was needed in the reported successful test.
- Movement, lumbar, heat on/off and massage controls were exercised on the chair.
  Half-second headrest and footrest button bursts previously improved movement
  with shorter taps. Later diagnostics showed a half-second footrest press
  without forward feedback while slider movement worked. The owner subsequently
  confirmed that rc3 works. Later rc6 diagnostics recorded a 0.301-second
  post-wake interval before Memory 1. The later rc8 Memory 3 check succeeded;
  the cause of earlier intermittent recall remains unconfirmed.
- The idempotent activation command turns the wired controller on when needed;
  repeating it does not toggle it off.
- Status notifications reflect changes made with the physical controls. A
  successful Bluetooth write alone does not prove physical movement.
- Massage set to 15 minutes stopped at approximately 15 minutes. Repeating that
  setting about five minutes into a session extended operation to approximately
  20 minutes without an observed interruption.
- Heat set to 60 seconds stopped at approximately 60 seconds. Repeating that
  setting after 30 seconds extended operation to approximately 90 seconds.
- Zero timer values did not establish untimed operation. Continuous is implemented
  by Home Assistant renewing supported native durations.

Heat and massage renewal were demonstrated separately. The longer schedules
below, combined operation, and the new memory/session integration still require
physical validation. Automated checks cannot replace those timing tests.

## Continuous operation

| Function | Native duration | Renewal interval |
| --- | --- | --- |
| Heat | 180 minutes | 175 minutes |
| Massage | 45 minutes | 40 minutes |

Each renewal is one command with a five-minute margin before native expiry. These
intervals adapt the physically demonstrated shorter-duration renewal behavior.
They are not claims of a firmware timer-disable feature.

Saved choices survive reconnects and restarts. Active sessions do not resume
without an explicit user start. Selecting Continuous while a function is Off
only arms the choice. Off, lost connection, stale feedback or a failed renewal
ends the session; the last native timer remains responsible for automatic shutoff.
Automatic renewal never activates the chair.

Heat renewal is deferred while finite or unmanaged massage is active, because
an ordinary heat command can also renew the copied massage setting. A selected
Continuous massage choice alone does not count as an active managed session.
Heat must still be On when renewal becomes possible; it is never relit after an
observed Off. Independent heat countdown preservation during massage renewal
remains a physical-validation item.

The Continuous status sensors describe Home Assistant's renewal session, not a
separate measurement of the heater or massage mechanism. Waiting, Starting,
Running and Stopped therefore remain distinct from the feature's On/Off state.

## Memory and request handling

New memories store both Continuous choices. Older profiles without these flags
remain finite and are not rewritten on load. Recall sends one target frame with
the appropriate native durations; renewal workers attach only after the target
is confirmed and the choices are saved. The initial renewal delay accounts for
elapsed recall time. Saved-Off functions retain the choice without starting.
Superseded recalls, disconnects, changed presets and Off reports cannot attach
obsolete workers after a pending storage write.

Fresh installations start with empty slots. Existing saved profiles and legacy
migration markers are retained. Saving one slot preserves unreadable or
newer-format entries in other slots; only an explicitly selected slot is replaced.
An unsupported overall saved structure blocks writes. Recalls that fail before
target dispatch preserve existing Continuous sessions and choices. Public code contains no chair-address-specific
profile imports or personal Bluetooth address defaults.

Set memory requires fresh stationary feedback. Other user controls are unavailable
while saving is armed, and remain locked through storage completion. Memory
buttons select the destination and disarm saving afterward. Stop and replacement
requests retain cancellation and ordering checks. Newer memory requests supersede
older pending requests.

The `svago.recall_memory` action accepts exactly one Memory button entity and
rejects saving, ambiguous targets and other button types. Re-arming Set memory
during storage lookup invalidates an older recall. Its integration activity is
recorded as Recall, but it does not update HA's button last-pressed timestamp;
neither record proves physical movement.

Isolated headrest, seat and footrest taps use a fixed 0.5 seconds; backrest uses
a fixed 0.3 seconds. None extends for a reported position increment. Same-direction
taps join an active burst with a three-second maximum from its start. A timed
pulse without a position increment does not establish whether physical movement
occurred. Feedback validity and interruption safeguards remain active.
Reverse, another axis, a slider, memory or Stop use the
existing stop-before-handoff behavior. Observed endpoints and already-reached
slider targets are quiet no-ops. Genuine transport, feedback and stop failures
remain visible in diagnostics and service results.

## Connection and portability

Ready requires a connected transport, active notification subscription, fresh
usable status, completed setup and effective capacity for the 27-byte command.
A connection flag by itself is insufficient. Connection status, Last action and
Last problem remain readable when controls are unavailable. Detailed reports
retain transport history and command outcomes while redacting device addresses.

Normal use requires a powered chair and an available Bluetooth connection.
Movement buttons, sliders, lumbar adjustments, presets, memory recall, Heat On
and non-Off massage automatically activate the controller when needed. Memory
saving, heat-level/timer settings, Off and Stop do not wake it. Automations wait
for Ready before their requested action; there is no separate Turn on button.
Setup, diagnostic publishing and preference restoration introduce no activation
or movement writes.

If required services are missing, setup clears that client's service cache and
tries once with cached services disabled. It resolves the device again and waits
for fresh usable status. A failed or unsupported cache clear does not prevent
that single uncached attempt; cancellation or unload prevents another connection.
Diagnostics retain the cache-clear result and missing-service error. Persistent
failure returns to the existing setup or reconnect retry mechanism.

## Recorded limits and release checks

The owner confirmed rc14's requested headrest and footrest adjustment, and 1.1.1
retains its direction and Home behavior. The
[Home and direction guide](docs/HOME-TEST.md) records the released timings and
successful Home checks; promotion to stable does
not require repeating those checks. These reports do not establish a calibrated
physical step, every chair/firmware combination, or the longer Continuous timing
combinations described above.
Sliders still target whole reported positions. Fractional targets or readback
have not been verified. Rescaling their displayed numbers would not add precision,
and this build does not change their range or step.
That extra settling interval is armed by a Home command from HA. Physical
controller or remote Off is not detected and does not itself arm the interval;
startup reliability through that path remains unresolved.

The successful rc8 checks do not establish repeated Home, every subsequent
control after Stop, or every comfort/settings handoff. Those remain recorded
limits; they are not part of the direction-pulse check. Longer renewal
schedules and combined heat/massage behavior retain their existing
physical-validation limits.

Require successful configured GitHub validation workflows for the exact source
revision before publishing a stable release.
