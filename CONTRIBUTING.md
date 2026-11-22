# Contributing

Report reproducible problems with the chair model, integration and Home Assistant
versions, Bluetooth connection type, and the physical result. Connection status,
Last action, Last problem, and the downloadable diagnostics usually give more
useful evidence than screenshots of a pressed button.

Keep changes focused. Include a regression test when changing command ordering,
storage, feedback handling, timer renewal, or cancellation. Tests simulate the
chair; passing them does not establish a new physical command or model's behavior.
Keep captured evidence distinct from inferred behavior, and do not add guessed
movement commands or arbitrary retries.

## Development

Use a separate Python 3.14.10 environment; do not install development dependencies
inside a running Home Assistant installation.

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

The pinned test dependencies provide the minimum supported Home Assistant runtime.
Repeat the checks in a separate environment using `requirements-test-current.txt`
for the current supported release, Home Assistant 2026.11.2. Update that file and
the CI matrix when validating a newer stable release. The GitHub workflows test
both versions and run Ruff, hassfest, and the Marketplace/HACS checks.

Existing entity identifiers and saved memory data must remain usable across an
upgrade. A new installation starts with all three memory slots empty.
The normal setup assumes the chair is powered and connects through Home Assistant
Bluetooth. Reconnection must not replay a previous adjustment or start comfort
functions merely because Continuous remains selected.

Review logs before attaching them to public issues. Keep raw captures, private
development checkpoints, personal addresses, and Home Assistant storage files
out of the repository. Share a small sanitized example when it is needed.

See [Publishing](docs/PUBLISHING.md) for the release process.
