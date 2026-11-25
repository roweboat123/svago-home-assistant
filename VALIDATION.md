# Validation — Svago 1.1.0rc1

## Scope

Prepared November 24, 2026. The integration was developed against a Motion Mastery
chair using captured Bluetooth traffic and physical command tests. Automated
checks exercise Home Assistant behavior with simulated transport and feedback;
they do not establish behavior on other chair models, firmware or adapters.

Local validation passed on both Home Assistant **2026.10.0** and **2026.11.2**:
**1,033 tests** on each, plus Ruff lint and formatting.
Five dependency deprecation warnings remain on each version. The 34 added cases
cover the exact native Home frame, captured nonmonotonic travel, fresh feedback
after write completion, stable neutral detection, interruption, reconnects,
Continuous session handling, independent button availability and detached
diagnostics. Existing tests retain coverage for memory-data preservation, fresh
comfort acknowledgments and elapsed lumbar deadlines. Reproduce these checks with
[CONTRIBUTING.md](CONTRIBUTING.md).

This is a testing candidate. The exact native Home packet was captured from the
remote and then replayed independently from a phone. The owner confirmed that
the chair returned home to neutral, with Bluetooth status notifications
continuing afterward. The new Home Assistant button still requires an on-chair
test before a stable release; see [the Home test guide](docs/HOME-TEST.md).

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
- Heat and massage were already Off during that replay. Their On-to-Off
  transitions still need testing through the new HA button. The owner confirmed
  that holding the side dial and switching the remote Off also return home and
  turn the wired controller Off; controller power is not independently observable
  in the captured status fields.
- The owner confirmed seamless reconnection with the power-cycle recovery fix.
  No manual integration reload was needed in the reported successful test.
- Movement, lumbar, heat on/off and massage controls were exercised on the chair.
  Half-second headrest and footrest button bursts resolved the observed lack of
  movement with shorter taps. Backrest taps retain quarter-second timing.
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
start of that burst. Reverse, another axis, a slider, memory or Stop use the
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
The optional startup example waits for Ready before pressing Turn on.
Setup, diagnostic publishing and preference restoration introduce no activation
or movement writes.

If required services are missing, setup clears that client's service cache and
tries once with cached services disabled. It resolves the device again and waits
for fresh usable status. A failed or unsupported cache clear does not prevent
that single uncached attempt; cancellation or unload prevents another connection.
Diagnostics retain the cache-clear result and missing-service error. Persistent
failure returns to the existing setup or reconnect retry mechanism.

## Remaining physical and release checks

1. Install this candidate and test Return to neutral, wired-controller shutdown,
   active heat/massage shutdown and Stop during Home travel using the
   [Home test guide](docs/HOME-TEST.md).
2. Check Continuous
   status changes and a saved-memory recall with Continuous selected.
3. Observe the longer renewal schedules and combined heat/massage behavior.
4. Require successful configured GitHub validation workflows for the exact source
   revision before publishing a stable release.
