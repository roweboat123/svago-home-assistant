# Validation — Svago 1.1.0rc4

## Scope

Prepared November 24, 2026. The integration was developed against a Motion Mastery
chair using captured Bluetooth traffic and physical command tests. Automated
checks exercise Home Assistant behavior with simulated transport and feedback;
they do not establish behavior on other chair models, firmware or adapters.

Local validation of 1.1.0rc4 passed on both Home Assistant **2026.10.0** and **2026.11.2**:
**1,079 tests** on each, plus Ruff lint and formatting.
Five dependency deprecation warnings remain on each version. Coverage includes
bounded directional startup, fresh feedback after the post-Home wake interval,
interruption and reconnect handling, removal of the standalone Turn on entity,
and the readiness-only automation example with its guarded caller. Existing
tests retain the exact native Home frame, captured nonmonotonic travel, stable
neutral detection, Continuous sessions, memory-data preservation, fresh comfort
acknowledgments and elapsed lumbar deadlines. The preceding rc1 baseline passed
1,033 tests on each Home Assistant version. Reproduce these checks with
[CONTRIBUTING.md](CONTRIBUTING.md).

The exact native Home packet was captured from the
remote and then replayed independently from a phone. The owner confirmed that
the chair returned home to neutral, with Bluetooth status notifications
continuing afterward. The owner subsequently confirmed that the Home Assistant
button worked in 1.1.0rc1.

Version 1.1.0rc4 retains Return to neutral under Configuration and the removal of
the standalone Turn on button. Home's entity identity, native command and completion
checks are unchanged. This remains a release candidate; 1.0.2 is the latest
stable release.

The new directional-startup handling addresses diagnostics where a half-second
footrest press showed no forward position feedback while a slider moved using
the same direction commands. This supports a startup-timing hypothesis without
establishing the cause. The candidate bounds startup waiting to two seconds and
keeps the three-second absolute burst cap. The owner subsequently confirmed that
1.1.0rc3 works; the physical cause remains unproven.

The owner reports that the first memory press after Home only wakes the wired
controller and the second performs recall, regardless of time spent waiting
after Home. One diagnostic capture contains a neutral Memory 1 recall followed
by a successful nonneutral Memory 3 recall, so it does not capture that failure.
In a separate controlled check, the owner went directly from Home to Memory 3
and confirmed that the first press only woke the controller; the second moved it.
The candidate tracks a Home write attempt, then waits for a fresh report after
a one-second settling interval following the next foreground wake acknowledgment.
Healthy feedback completes this wait in about one second; the existing
three-second stale-feedback limit gives a four-second total bound on this wait.
The wake command and dependent memory target each remain single sends. Normal
actions without a preceding Home retain their timing; Home, reconnects and passive
status never start a wake on their own. The owner confirmed rc3 works with its
two-second interval. Version 1.1.0rc4 shortens only this interval to one second;
the shorter timing still needs an on-chair check.

The owner previously reported completing internal testing of the fixes supplied
for stable 1.0.2. This candidate retains those fixes.

The owner confirmed seamless reconnection on the affected chair with `1.0.1rc1`
after reporting the missing-FFF0 setup error following power loss. Version 1.0.2
retains the connection code from 1.0.1. This validates recovery on that installation;
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
- Heat and massage were already Off during the phone replay. The owner confirmed
  that holding the side dial and switching the remote Off also return home and
  turn the wired controller Off; controller power is not independently observable
  in the captured status fields.
- The owner confirmed seamless reconnection with the power-cycle recovery fix.
  No manual integration reload was needed in the reported successful test.
- Movement, lumbar, heat on/off and massage controls were exercised on the chair.
  Half-second headrest and footrest button bursts previously improved movement
  with shorter taps. Later diagnostics showed a half-second footrest press
  without forward feedback while slider movement worked. The owner subsequently
  confirmed that rc3 works; the shorter rc4 wake interval remains untested.
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

Same-direction taps join an active burst, with a three-second maximum from the
start of that burst. Without forward progress, the normal pulse can extend only
until the first forward report or the two-second startup deadline. Joined
presses cannot extend that deadline. No progress produces a released command
and an explicit failure. Reverse, another axis, a slider, memory or Stop use the
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

The shorter one-second wake interval needs an on-chair check of first-press
recall after Home. The [candidate guide](docs/HOME-TEST.md) gives that focused
check; unrelated long Continuous timing tests are not part of this check.

The successful HA Home test does not separately establish each interruption or
comfort-function case. The [Home guide](docs/HOME-TEST.md) retains optional checks
for collecting that additional evidence; Home's category change does not require
repeating its successful test. Longer renewal schedules and combined
heat/massage behavior retain their existing physical-validation limits.

Require successful configured GitHub validation workflows for the exact source
revision before publishing a stable release.
