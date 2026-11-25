# Changelog

## 1.1.0rc12 — 2026-11-24

- Give **Seat tilt up** and **Seat tilt down** the same fixed 0.25-second pulse as
  backrest, without extending an isolated press to wait for a position increment.
- Record the owner's successful rc11 backrest button check. Seat's new timed
  behavior remains pending a chair check; neither timing defines a fixed step.
- Retain headrest/footrest startup handling, Home behavior, the separate
  quarter-second wake interval, slider precision and existing entity identities.

Version 1.0.2 remains the latest stable release.

## 1.1.0rc11 — 2026-11-24

- Set an isolated backrest press to a fixed 0.25-second pulse, without extending
  it to wait for a reported position increment. Retain Stop, feedback checks and
  the three-second cap on repeated presses.
- Keep headrest/footrest and seat timing, their bounded startup handling, the
  restored seat controls and the confirmed Home handoff behavior unchanged.
- Keep the separate 0.25-second post-Home wake interval unchanged.

Physical response to the quarter-second backrest pulse still needs confirmation.
It does not guarantee a fixed physical step or establish a fix for excessive
movement. Version 1.0.2 remains the latest stable release.

## 1.1.0rc10 — 2026-11-24

- Restore rc8's backrest behavior: a nominal 0.1-second pulse with startup waiting
  until first forward feedback, bounded by two seconds when needed. The overall
  burst cap remains three seconds.
- Retain rc9's **Seat tilt up** and **Seat tilt down**, their original entity
  identities, and the confirmed Home handoff behavior.
- Record the owner's rc9 result: 50 ms backrest taps produced no movement or
  error, while the slider worked. The firmware cause and smaller physical-step
  calibration remain unresolved. This recovery candidate does not fix the
  previously reported excessive adjustment size.

Confirm that backrest responds again and capture diagnostics after one working
press before further timing changes. Version 1.0.2 remains the latest stable release.

## 1.1.0rc9 — 2026-11-24

- Change backrest buttons to a requested 0.05-second tap, releasing without
  waiting for the first position-progress report. This halves nominal duration
  but does not guarantee half the physical movement.
- Restore **Seat tilt up** and **Seat tilt down** with their
  original entity keys. Keep the seat pulse at 0.25 seconds and other axes'
  existing startup handling.
- Record the owner's successful rc8 queued Footrest raise, one-press Memory 3
  recall, latest-choice replacement and Stop cancellation checks during Home,
  plus fresh Footrest raise after Stop. Retain Home handling, wake timing and
  saved memories. The new backrest tap and restored seat controls await chair checks.

Version 1.0.2 remains the latest stable release.

## 1.1.0rc8 — 2026-11-24

- Let native Home complete before the latest requested movement, recall, preset
  or comfort/settings action runs. New choices replace the waiting choice;
  repeated Home joins the current operation without resending it and clears the
  waiting follow-up. Stop interrupts Home and discards pending controls.
- Discard waiting controls if Home fails, times out, loses fresh feedback, or
  disconnects. Do not replay them after reconnecting.
- Shorten the normal backrest direction pulse from 0.25 to 0.1 seconds. Keep
  headrest and footrest pulses at 0.5 seconds and the seat pulse at 0.25 seconds.
- Retain the 0.1-second repeat cadence, two-second startup deadline and
  three-second burst cap. Without forward feedback, a pulse can still extend
  until the first forward report or the startup deadline; physical step size
  is not fixed.

The owner could raise the footrest with the wired controller in the failed HA
state, and HA raise worked after Home completed fully. These observations motivate
the Home handoff change; the firmware reason and candidate's physical success
remain unverified. Wake packets, wake settling time, stored memories and the
estimated-controller sensor are unchanged. Version 1.0.2 remains the latest stable
release.

## 1.1.0rc7 — 2026-11-24

- Add `svago.recall_memory` for exactly one Memory button. It can only recall,
  blocks while Set memory is armed or saving, and preserves newer-request ordering.
  Ordinary Memory buttons retain their save/recall behavior; existing automations
  are not migrated automatically.
- Preserve Continuous heat and massage sessions when a preset fails before its
  write begins; retire renewal when preset delivery is attempted.
- Preserve the required stop before the next control if a slider's final release
  delivery is uncertain on the same connection.
- Include the action's YAML metadata in the manual-install archive.

The quarter-second post-Home wake pause and estimated controller sensor are
unchanged. First-press recall reliability remains under investigation; this
candidate does not establish a fix for wake behavior after physical controller Off.
Version 1.0.2 remains the latest stable release.

## 1.1.0rc6 — 2026-11-24

- Shorten the pause after the first wake following Home to 0.25 seconds.
  Require fresh feedback after that interval; normal feedback cadence may make
  the wait about 0.3 seconds, with a default maximum of 3.25 seconds. The shorter
  interval remains a candidate pending an on-chair check.
- Retain the two-second Home neutral and directional startup limits, three-second
  directional burst cap, memory behavior and estimated controller sensor.
- First-press memory recall after Home remains intermittently unreliable in the
  owner's report. The owner will check the automation internally; the cause and
  effect of the shorter pause remain unverified.

Version 1.0.2 remains the latest stable release.

## 1.1.0rc5 — 2026-11-24

- Add the default-enabled **Wired controller (estimated)** diagnostic sensor.
  Estimate On after a successful HA wake and Off after successful HA Return to
  neutral; show Unknown when not ready, after reconnect or unusable status, and
  while wake/Home is in progress or incomplete. Do not restore it after restarting.
- Keep the estimate separate from actual power feedback, which has not been
  verified. Physical controls and the Bluetooth remote can make it inaccurate.
  It does not gate actions, skip wake commands or change packets or timing.
- Retain rc4's one-second wake interval; its physical timing check is still pending.

Version 1.0.2 remains the latest stable release.

## 1.1.0rc4 — 2026-11-24

- Record the owner's confirmation that 1.1.0rc3 works.
- Shorten the settling interval after the first wake following Home from two
  seconds to one, with fresh feedback still required and a four-second maximum
  including notification cadence. The shorter interval needs an on-chair check.
- Keep the two-second Home neutral-stability and directional startup limits.

Version 1.0.2 remains the latest stable release.

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
