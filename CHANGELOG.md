# Changelog

## 1.1.0rc3 — 2026-11-24

- Add bounded startup waiting to directional presses when no forward movement
  has been reported. After the normal interval, stop on the first forward report
  or fail after the two-second startup bound. Retain the three-second absolute
  burst cap. The diagnostic pattern suggests startup timing, but the
  cause is not proven and this candidate still needs an on-chair check.
- After a Home command, wait for fresh feedback following a two-second settling
  interval after the first subsequent wake acknowledgment, before its requested
  action. The wait is bounded to five seconds including notification cadence.
  Keep a single wake command and single memory target; do not retry either.
  This addresses the reported first-press memory behavior as a candidate,
  with the physical cause and result still unconfirmed.
- Remove the standalone **Turn on** button. Movement, sliders, lumbar, presets,
  memory recall, Heat On and non-Off massage already activate the controller.
  Memory saves, settings, Off and Stop do not wake it.
- Retain **Return to neutral** under Configuration and the owner's successful
  1.1.0rc1 Home test.
- Replace the old startup script with a readiness-only wait. It sends no chair
  command; callers check Ready before their chosen next action.

Version 1.0.2 remains the latest stable release.

## 1.1.0rc2 — 2026-11-24

Prepared but not delivered; superseded by rc3.

- Move **Return to neutral** from Controls to Configuration, retaining its entity
  identity, native command and behavior.
- Record the owner's successful Home Assistant test of 1.1.0rc1. This update
  changes only the button category, version metadata and documentation.

Version 1.0.2 remains the latest stable release.

## 1.1.0rc1 — 2026-11-24

Testing candidate adding the native **Return to neutral** button.

- Send the exact captured Home command once, without activation, a memory-slot
  recall or an intentional Bluetooth disconnect. The owner verified this command
  returns the chair home with the phone still connected.
- Stop active Continuous heat and massage renewal while preserving saved choices.
- Require fresh feedback confirming neutral reported positions and comfort
  functions Off. Retain Stop, replacement, stale-feedback and timeout handling.
- Export the latest Home request in diagnostics. This candidate was supplied for
  on-chair validation; the owner subsequently reported success, as recorded above.

## 1.0.2 — 2026-11-24

Fix issues found and tested during internal testing.

- Preserve unreadable or newer-format memory slots when another slot is saved.
  Replace an unreadable slot only when explicitly saving that slot, and block
  writes when the overall saved structure cannot be interpreted safely.
- Require fresh matching chair feedback after heat and massage commands instead
  of treating a previously matching state as confirmation.
- Keep existing Continuous sessions and choices when a memory recall fails
  before sending its target. Retire renewal workers when target delivery begins.
- Bound lumbar adjustments by elapsed time, including Bluetooth write delays.
  Attempt release when a write times out, and require a Stop before the next
  control if release delivery was uncertain on the same connection.

## 1.0.1 — 2026-11-22

Fix automatic reconnection after the chair is powered off and back on.

- If the chair connects without its required FFF0 service or FFF1/FFF2
  characteristics, clear that client's service cache and attempt fresh discovery
  once. Resolve the chair through Home Assistant again before reconnecting.
- Continue with the uncached attempt if clearing is unsupported or fails, and
  expose the cache-recovery outcome in diagnostics. Stop on cancellation or unload.
- Wait for fresh chair status on the replacement connection, including when cache
  clearing produces a disconnect callback. Recovery sends no chair commands and
  never replays an earlier adjustment.

The owner confirmed seamless reconnection on the affected chair with the
`1.0.1rc1` candidate; this release retains that tested connection code. Existing
saved memories and timer choices are preserved. See [the recovery check](docs/RECONNECT-TEST.md).

## 1.0.0 — 2026-11-22

First public release of Svago Motion Mastery.

- Add visible heat and massage Continuous session status, distinguishing a saved
  choice from active renewal or a stopped session.
- Save Continuous choices with each Home Assistant memory. Recall restores them
  after reaching the saved target, without a second movement command. Existing
  memories retain finite timers unless saved again.
- Retain supported native timers: Continuous heat renews a 180-minute timer every
  175 minutes; Continuous massage renews a 45-minute timer every 40 minutes.
- Preserve connection readiness diagnostics, quiet endpoint no-ops, memory-save
  control locking, automatic activation before adjustments and smooth repeated
  directional taps.
- Support ordinary always-powered installations. Include an optional script that
  waits for readiness before turning on the chair controller.
- Remove personal address defaults and captured profile imports from public code.
  Existing saved memories remain intact; new installations start with empty slots.
- Add transparent chair-only artwork for light and dark themes, MIT code licensing,
  public documentation, repository checks and Marketplace/HACS configuration.

Catalog inclusion is subject to review. See [validation](VALIDATION.md) for the
boundary between automated checks and physical
chair testing, and [publishing](docs/PUBLISHING.md) for release steps.
