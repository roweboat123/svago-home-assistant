# Changelog

## 1.0.1rc1 — Test candidate

- If the chair connects without its required FFF0 service or FFF1/FFF2
  characteristics, clear that client's service cache and attempt fresh discovery
  once. Resolve the chair through Home Assistant again before reconnecting.
- Continue with the uncached attempt if clearing is unsupported or fails, and
  expose the cache-recovery outcome in diagnostics. Stop on cancellation or unload.
- Wait for fresh chair status on the replacement connection, including when cache
  clearing produces a disconnect callback. Recovery sends no chair commands and
  never replays an earlier adjustment.

This candidate addresses the reported missing-FFF0 setup error after power loss.
Automated recovery checks pass; confirming the cause and recovery on the affected
chair remains necessary before a stable release. See [the test procedure](docs/RECONNECT-TEST.md).

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
