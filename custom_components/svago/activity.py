"""Bounded action outcomes, separate from Bluetooth and physical movement."""

import time
from collections import Counter, deque
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime

OUTCOME_LABELS = {
    "in_progress": "In progress",
    "completed": "Request completed",
    "at_observed_limit": "Already at limit",
    "superseded": "Replaced by newer request",
    "cancelled": "Cancelled",
    "failed": "Failed",
    "blocked": "Finish setting memory first",
}


class ActionActivity:
    """Keep the newest request visible even when an older request finishes later."""

    def __init__(self, changed: Callable[[], None] = lambda: None) -> None:
        self._changed = changed
        self._sequence = 0
        self._history: deque[dict] = deque(maxlen=20)
        self._last_problem: dict | None = None
        self._counts: Counter = Counter()

    def begin(self, action: str, request=None) -> dict:
        self._sequence += 1
        record = {
            "id": self._sequence,
            "action": action,
            "request": request,
            "outcome": "in_progress",
            "started_at": datetime.now(UTC).isoformat(),
            "_started_monotonic": time.monotonic(),
        }
        self._history.append(record)
        self._changed()
        return record

    def finish(self, record: dict, outcome: str, error: str | None = None) -> None:
        record["outcome"] = outcome
        record["completed_at"] = datetime.now(UTC).isoformat()
        record["duration_seconds"] = round(time.monotonic() - record["_started_monotonic"], 3)
        self._counts[outcome] += 1
        if error is not None:
            record["error"] = error[:2048]
        if outcome in ("failed", "blocked"):
            self._last_problem = record
        self._changed()

    @staticmethod
    def _public(record: dict | None) -> dict | None:
        if record is None:
            return None
        return deepcopy({key: value for key, value in record.items() if not key.startswith("_")})

    @property
    def last_action(self) -> dict | None:
        return self._public(self._history[-1]) if self._history else None

    @property
    def last_problem(self) -> dict | None:
        return self._public(self._last_problem)

    def snapshot(self) -> dict:
        return {
            "requests_total": self._sequence,
            "outcomes": dict(self._counts),
            "last_action": self.last_action,
            "last_problem": self.last_problem,
            "history": [self._public(record) for record in self._history],
        }
