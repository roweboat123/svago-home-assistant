"""State-preserving commands, independent of Home Assistant and BLE transport."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from math import ceil

from .memory_protocol import MemoryProfile
from .protocol import (
    ACTIVATION_FIELD,
    CONTINUOUS_HEAT_MINUTES,
    CONTINUOUS_MASSAGE_MINUTES,
    HEAT_LEVELS,
    HEAT_ZONES,
    HOME_COMMAND,
    MASSAGE_MODES,
    OBSERVED_BOUNDS,
    POSITION_NAMES,
    TIMER_OPTIONS,
    Status,
    preset_command,
)

_LOGGER = logging.getLogger(__name__)
# Headrest and footrest use the physically confirmed longer burst after
# nonresponsive 0.25 s presses, retaining their startup feedback allowance.
# Backrest and seat use quarter-second timed pulses without extending them
# to wait for quantized position feedback.
MOTOR_PULSE_SECONDS = (0.5, 0.25, 0.25, 0.5)
MOTOR_REPEAT_SECONDS = 0.1
LUMBAR_BURST_SECONDS = 3.0
# Repeated taps extend the current gesture, never a backlog of future travel.
MOTOR_BURST_MAX_SECONDS = 3.0
# Seat travel above 31 is now captured, including ZGR at 40. Allow a full
# observed-range move while retaining the two-second no-progress guard.
SLIDER_MAX_SECONDS = 60.0
SLIDER_STALL_SECONDS = 2.0
STARTUP_FEEDBACK_SECONDS = 2.0
# Quantized position feedback can stay unchanged for about a second while
# moving, including when movement was started with the physical remote.
MEMORY_STABLE_SECONDS = 2.0
MEMORY_MAX_SECONDS = 60.0
MEMORY_STALL_SECONDS = 3.0
MEMORY_POLL_SECONDS = 0.1
HOME_MAX_SECONDS = 60.0
HOME_STABLE_SECONDS = 2.0
HOME_POLL_SECONDS = 0.1
# A foreground wake after native Home needs time for the controller to settle.
# This bounded interval is a test candidate, not a decoded readiness flag.
POST_HOME_WAKE_SECONDS = 0.25
POST_HOME_WAKE_POLL_SECONDS = 0.1
MASSAGE_RENEW_SECONDS = 2400.0
MASSAGE_DEFER_SECONDS = 1.0
HEAT_RENEW_SECONDS = 10500.0
HEAT_DEFER_SECONDS = 1.0


class ControlError(Exception):
    """A command could not be completed without guessing current state."""


class CommandSuperseded(Exception):
    """A newer user request replaced this request; not a user-facing error."""


@dataclass(frozen=True)
class _OperationRequest:
    """Keep ordering and the Home outcome across locks and memory storage reads."""

    generation: int
    request_id: int
    intent_sequence: int
    home_completion: asyncio.Future[bool] | None


def _check_movement_feedback(position, start, best, direction, bounds, elapsed):
    """Allow the captured one-unit startup excursion, not sustained reverse travel."""
    lower, upper = bounds
    if not lower <= position <= upper:
        raise ControlError("Unexpected position feedback; movement stopped.")
    progress = (position - best) * direction
    if progress < 0 and (
        best != start or (position - start) * direction < -1 or elapsed >= STARTUP_FEEDBACK_SECONDS
    ):
        raise ControlError("Unexpected position feedback; movement stopped.")
    return position if progress > 0 else best


class Controller:
    def __init__(
        self,
        write: Callable[[bytes], Awaitable[None]],
        changed: Callable[[], None] = lambda: None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stale_after: float = 3.0,
        feedback_timeout: float = 8.0,
        preferences_changed: Callable[[dict[str, bool]], None] = lambda _choices: None,
        preferences_flush: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._write = write
        self._changed = changed
        self._clock = clock
        self._sleep = sleep
        self._stale_after = stale_after
        self._feedback_timeout = feedback_timeout
        self._preferences_changed = preferences_changed
        self._preferences_flush = preferences_flush
        self._received_at = float("-inf")
        self._updated = asyncio.Event()
        self._active: asyncio.Task | None = None
        self._transition = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._settings_epoch = 0
        self._request_id = 0
        self._intent_sequence = 0
        self._superseded: set[asyncio.Task] = set()
        self._needs_stop = False
        self._generation = 0
        self._status_sequence = 0
        self._stable_positions: tuple[int, ...] | None = None
        self._positions_stable_since = float("inf")
        self._stable_position_reports = 0
        self.connected = False
        self.status: Status | None = None
        self.last_heat_request: dict | None = None
        self.last_activation: dict | None = None
        self.last_home_request: dict | None = None
        self._wired_controller_power: str | None = None
        self._wired_controller_power_basis = "No completed HA wake or Return to neutral command"
        self._home_session: dict | None = None
        self._home_completion: asyncio.Future[bool] | None = None
        self._home_wake_pending = False
        self._home_wake_epoch = 0
        self.setting_requests: deque[dict] = deque(maxlen=5)
        self.position_requests: deque[dict] = deque(maxlen=4)
        self.tap_requests: deque[dict] = deque(maxlen=4)
        self._tap_session: dict | None = None
        self._tap_sequence = 0
        self.preset_requests: deque[dict] = deque(maxlen=4)
        self.memory_requests: deque[dict] = deque(maxlen=4)
        self._massage_continuous = False
        self._massage_session = 0
        self._massage_running = False
        self._massage_starting = False
        self._massage_starting_active = False
        self._massage_starting_timer: bytes | None = None
        self._massage_selector: int | None = None
        self._massage_renewal_task: asyncio.Task | None = None
        self._massage_tasks: set[asyncio.Task] = set()
        self._massage_renewal_trace: dict = {"outcome": "idle", "renewal_count": 0}
        self._heat_continuous = False
        self._heat_session = 0
        self._heat_running = False
        self._heat_starting = False
        self._heat_starting_active = False
        self._heat_selector: int | None = None
        self._heat_expected_timer: bytes | None = None
        self._heat_pending_timer: bytes | None = None
        self._heat_renewal_task: asyncio.Task | None = None
        self._heat_tasks: set[asyncio.Task] = set()
        self._heat_renewal_trace: dict = {"outcome": "idle", "renewal_count": 0}

    @property
    def available(self) -> bool:
        return bool(
            self.connected
            and self.status
            and self.status.usable
            and self._clock() - self._received_at <= self._stale_after
        )

    @property
    def wired_controller_power_estimate(self) -> dict:
        """Describe HA's command-based estimate, never measured controller power.

        Physical controls can change the wired controller without any decoded
        Bluetooth power feedback. This estimate must not gate control commands.
        """
        available = self.available
        return {
            "state": self._wired_controller_power if available else None,
            "estimated": True,
            "basis": (
                self._wired_controller_power_basis if available else "Chair feedback unavailable"
            ),
            "physical_controls_observed": False,
        }

    def _set_wired_controller_power_estimate(
        self, state: str | None, basis: str, *, generation: int | None = None, notify=True
    ) -> None:
        if generation is not None and generation != self._generation:
            return
        before = self.wired_controller_power_estimate
        self._wired_controller_power = state
        self._wired_controller_power_basis = basis
        if notify and self.wired_controller_power_estimate != before:
            self._changed()

    @property
    def status_age_seconds(self) -> float | None:
        """Age of the last accepted status on the current connection."""
        if self.status is None:
            return None
        return max(0.0, self._clock() - self._received_at)

    @property
    def status_freshness_limit(self) -> float:
        """Maximum status age accepted before sending a command."""
        return self._stale_after

    @property
    def busy(self) -> bool:
        return self._active is not None or self._transition.locked()

    @property
    def intent_sequence(self) -> int:
        """Sequence of requested chair actions, including queued setting changes."""
        return self._intent_sequence

    @property
    def massage_continuous(self) -> bool:
        """Remembered choice; only an explicit start creates a renewal session."""
        return self._massage_continuous

    @property
    def timer_preferences(self) -> dict[str, bool]:
        return {"heat": self._heat_continuous, "massage": self._massage_continuous}

    def restore_timer_preferences(self, choices: dict[str, bool]) -> None:
        """Restore choices before connection without starting either function."""
        if set(choices) != {"heat", "massage"} or any(
            type(value) is not bool for value in choices.values()
        ):
            raise ValueError("Both timer preferences must be booleans")
        self._stop_heat_session("preferences_restored")
        self._stop_massage_session("preferences_restored")
        self._heat_continuous = choices["heat"]
        self._massage_continuous = choices["massage"]
        self._changed()

    async def _flush_timer_preferences(self) -> None:
        if self._preferences_flush is not None:
            await self._preferences_flush()

    @property
    def massage_renewal_diagnostics(self) -> dict:
        return {
            "selected": self._massage_continuous,
            "active": self._massage_running,
            "interval_seconds": MASSAGE_RENEW_SECONDS,
            "native_minutes": CONTINUOUS_MASSAGE_MINUTES,
            **self._massage_renewal_trace,
        }

    def _stop_massage_session(self, reason: str, *, clear_selection=False) -> asyncio.Task | None:
        """Invalidate delayed work synchronously before a newer request can wait."""
        task = self._massage_renewal_task
        had_session = self._massage_running or self._massage_starting or task is not None
        changed = clear_selection and self._massage_continuous
        self._massage_session += 1
        self._massage_running = False
        self._massage_starting = False
        self._massage_starting_timer = None
        self._massage_renewal_task = None
        if clear_selection:
            self._massage_continuous = False
        if changed:
            self._preferences_changed(self.timer_preferences)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if had_session or changed:
            self._massage_renewal_trace["outcome"] = reason
            self._changed()
        return task

    def _massage_session_valid(self, generation: int, session: int) -> bool:
        return bool(
            self._massage_continuous
            and self._massage_running
            and generation == self._generation
            and session == self._massage_session
        )

    @property
    def heat_continuous(self) -> bool:
        """Remembered heat choice, separate from an active renewal session."""
        return self._heat_continuous

    @property
    def heat_renewal_diagnostics(self) -> dict:
        return {
            "selected": self._heat_continuous,
            "active": self._heat_running,
            "interval_seconds": HEAT_RENEW_SECONDS,
            "native_minutes": CONTINUOUS_HEAT_MINUTES,
            **self._heat_renewal_trace,
        }

    def _stop_heat_session(self, reason: str, *, clear_selection=False) -> asyncio.Task | None:
        """Invalidate only heat work before a newer command can wait."""
        task = self._heat_renewal_task
        had_session = self._heat_running or self._heat_starting or task is not None
        changed = clear_selection and self._heat_continuous
        self._heat_session += 1
        self._heat_running = False
        self._heat_starting = False
        self._heat_renewal_task = None
        self._heat_pending_timer = None
        if clear_selection:
            self._heat_continuous = False
        if changed:
            self._preferences_changed(self.timer_preferences)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if had_session or changed:
            self._heat_renewal_trace["outcome"] = reason
            self._changed()
        return task

    def _heat_session_valid(self, generation: int, session: int) -> bool:
        return bool(
            self._heat_continuous
            and self._heat_running
            and generation == self._generation
            and session == self._heat_session
        )

    @property
    def can_save_memory(self) -> bool:
        """Whether current observed state can be saved without touching the chair."""
        try:
            self.snapshot_memory()
        except ControlError:
            return False
        return True

    def _reset_position_stability(self) -> None:
        self._stable_positions = None
        self._positions_stable_since = float("inf")
        self._stable_position_reports = 0

    def set_connected(self, connected: bool) -> None:
        self._stop_massage_session("connection_changed")
        self._stop_heat_session("connection_changed")
        self._generation += 1
        self._settings_epoch += 1
        self._request_id += 1
        self._intent_sequence += 1
        self._needs_stop = False
        self._home_session = None
        if self._home_completion is not None:
            if not self._home_completion.done():
                self._home_completion.set_result(False)
            self._home_completion = None
        self.connected = connected
        self.status = None
        self._received_at = float("-inf")
        self._set_wired_controller_power_estimate(
            None, "Bluetooth connection changed", notify=False
        )
        self._reset_position_stability()
        self._updated.set()
        self._changed()

    def accept_status(self, status: Status) -> None:
        if not self.connected:
            return
        was_savable = self.can_save_memory
        changed = not self.available or self.status != status
        power_feedback_interrupted = self._wired_controller_power is not None and (
            not self.available or not status.usable
        )
        now = self._clock()
        if (
            self._stable_positions != status.positions
            or now - self._received_at > self._stale_after
        ):
            self._stable_positions = status.positions
            self._positions_stable_since = now
            self._stable_position_reports = 1
        else:
            self._stable_position_reports += 1
        self.status = status
        self._status_sequence += 1
        self._received_at = now
        if power_feedback_interrupted:
            # Resuming notifications cannot restore an estimate made before a
            # stale gap or unusable feedback. A new HA wake/Home must complete.
            self._set_wired_controller_power_estimate(
                None, "Chair feedback unavailable", notify=False
            )
        self._updated.set()
        self._observe_home_status(status, now)
        self._observe_tap_feedback(status, now)
        if self._massage_running or self._massage_starting:
            if (
                status.timer_minutes("massage") != CONTINUOUS_MASSAGE_MINUTES
                and not (self._massage_starting and status.raw[6:8] == self._massage_starting_timer)
            ) or status.raw[10] != self._massage_selector:
                self._stop_massage_session("chair_timer_or_preset_changed", clear_selection=True)
            elif status.massage in (None, "Off"):
                if self._massage_running or self._massage_starting_active:
                    self._stop_massage_session("chair_stopped")
            elif self._massage_starting:
                self._massage_starting_active = True
        if self._heat_running or self._heat_starting:
            if (
                status.raw[8:10] not in (self._heat_expected_timer, self._heat_pending_timer)
                or status.raw[10] != self._heat_selector
            ):
                self._stop_heat_session("chair_timer_or_preset_changed", clear_selection=True)
            elif status.heat is not True:
                if self._heat_running or self._heat_starting_active:
                    self._stop_heat_session("chair_stopped")
            elif self._heat_starting:
                self._heat_starting_active = True
        if changed or power_feedback_interrupted or was_savable != self.can_save_memory:
            self._changed()

    def snapshot_memory(self) -> MemoryProfile:
        """Copy fresh stationary feedback without activation, stops or BLE writes."""
        status = self._current(self._generation)
        if self.busy or status.raw[18] != 0:
            raise ControlError("Wait for the chair to stop before setting a memory.")
        if (
            self._stable_position_reports < 2
            or self._received_at - self._positions_stable_since < MEMORY_STABLE_SECONDS
        ):
            raise ControlError("Wait for stable chair position feedback before setting a memory.")
        try:
            return MemoryProfile.from_status(
                status,
                massage_continuous=self._massage_continuous,
                heat_continuous=self._heat_continuous,
            )
        except ValueError as exc:
            raise ControlError(str(exc)) from exc

    def arm_memory_save(self) -> None:
        """Reserve a stationary save without letting older queued controls resume."""
        self.snapshot_memory()
        # The connection locks new controls while armed. Also invalidate older
        # requests already waiting for a lock or disk access, before this
        # synchronous reservation returns. No moving action is cancelled here.
        self._settings_epoch += 1
        self._request_id += 1
        self._intent_sequence += 1

    def _current(self, generation: int) -> Status:
        if generation != self._generation or not self.available:
            raise ControlError(
                "Fresh chair status is unavailable. Wait for reconnection before trying again."
            )
        assert self.status is not None
        return self.status

    @asynccontextmanager
    async def _serialized_operation(self):
        """Confirm short setting changes in order; don't rebuild from old feedback."""
        generation = self._generation
        epoch = self._settings_epoch
        self._current(generation)
        # Register intent before waiting for another setting's acknowledgment.
        self._intent_sequence += 1
        # During Home, reserve before the settings lock so newer choices replace
        # older waiting settings instead of building a post-Home command queue.
        request = (
            self._reserve_operation(configuration=True)
            if self._home_completion is not None
            else None
        )
        if request is not None:
            await self._wait_for_home(request)
        async with self._settings_lock:
            # A queued action invalidated by failure, replacement or reconnect
            # is not another fresh command failure. Discard it before checking
            # readiness so one broken connection cannot produce a toast per waiter.
            if generation != self._generation or epoch != self._settings_epoch:
                raise CommandSuperseded
            self._current(generation)
            entered = False
            try:
                async with self._operation(configuration=True, request=request) as generation:
                    entered = True
                    yield generation
            except BaseException as exc:
                # A failed/unconfirmed setting must not be silently overwritten
                # by requests already waiting behind it. Only an obsolete Home
                # waiter that never entered can leave its newer choice intact.
                if (
                    entered
                    or request is None
                    or not isinstance(exc, (CommandSuperseded, asyncio.CancelledError))
                    or (
                        request.request_id == self._request_id
                        and request.intent_sequence == self._intent_sequence
                    )
                ):
                    self._settings_epoch += 1
                raise

    def _reserve_operation(self, *, configuration=False, interrupt_home=False):
        generation = self._generation
        self._current(generation)
        if not configuration:
            self._settings_epoch += 1
            self._intent_sequence += 1
        self._request_id += 1
        return _OperationRequest(
            generation,
            self._request_id,
            self._intent_sequence,
            None if interrupt_home else self._home_completion,
        )

    def _check_request(self, request: _OperationRequest) -> None:
        if (
            request.generation != self._generation
            or request.request_id != self._request_id
            or (
                request.home_completion is not None
                and request.intent_sequence != self._intent_sequence
            )
        ):
            raise CommandSuperseded

    async def _wait_for_home(self, request: _OperationRequest) -> None:
        self._check_request(request)
        if request.home_completion is not None:
            # A caller cancelling its follow-up must never cancel native Home.
            completed = await asyncio.shield(request.home_completion)
            self._check_request(request)
            if not completed:
                raise ControlError(
                    "Return to neutral did not complete; the waiting action was not sent."
                )

    @asynccontextmanager
    async def _operation(
        self, *, configuration=False, request=None, native_home=False, interrupt_home=False
    ):
        if request is None:
            request = self._reserve_operation(
                configuration=configuration, interrupt_home=interrupt_home
            )
        if not configuration:
            self._reset_position_stability()
        await self._wait_for_home(request)
        generation = request.generation
        task = asyncio.current_task()
        assert task is not None
        home_completion = None
        completed = False
        async with self._transition:
            self._check_request(request)
            if self._active is not None:
                self._needs_stop = True
            await self._cancel_active(superseded=True)
            self._check_request(request)
            self._current(generation)
            if self._needs_stop:
                # Finish the handoff before any new action. If this stop fails,
                # the replacement action must not start on top of old movement.
                await self._send(generation, motors=(0x80,) * 4, lumbar=0)
                self._needs_stop = False
            self._check_request(request)
            self._active = task
            if native_home:
                home_completion = asyncio.get_running_loop().create_future()
                self._home_completion = home_completion
            self._changed()
        try:
            yield generation
            completed = True
        except asyncio.CancelledError:
            if task in self._superseded:
                task.uncancel()
                raise CommandSuperseded from None
            raise
        finally:
            self._superseded.discard(task)
            if self._active is task:
                self._active = None
            if home_completion is not None:
                if self._home_completion is home_completion:
                    self._home_completion = None
                if not home_completion.done():
                    home_completion.set_result(completed and generation == self._generation)
            self._changed()

    async def _send(self, generation: int, **changes) -> None:
        # Re-read immediately before EVERY write, including the final release.
        payload = self._current(generation).command(**changes)
        await self._write_packet(payload)

    async def _write_packet(self, payload: bytes) -> None:
        try:
            async with asyncio.timeout(2):
                await self._write(payload)
        except (TimeoutError, OSError) as exc:
            raise ControlError("Bluetooth write failed; the action was not retried.") from exc

    async def _activate(self, generation: int, reason: str) -> None:
        """Send idempotent On and await a response, not a guessed power state."""
        trace = {"reason": reason, "outcome": "pending"}
        self.last_activation = trace
        try:
            wake_epoch = self._home_wake_epoch if self._home_wake_pending else None
            status = self._current(generation)
            trace["status_before_hex"] = status.raw.hex().upper()
            try:
                payload = status.activation_command()
            except ValueError as exc:
                raise ControlError(str(exc)) from exc
            trace["command_hex"] = payload.hex().upper()
            sequence = self._status_sequence
            self._set_wired_controller_power_estimate(
                None, "HA wake command in progress or incomplete", generation=generation
            )
            await self._write_packet(payload)
            trace["outcome"] = "waiting_for_response"
            await self._wait_for(
                generation,
                lambda status: (
                    self._status_sequence > sequence and status.raw[5] == ACTIVATION_FIELD
                ),
                timeout_message=(
                    "The chair did not acknowledge activation; the requested control was not sent."
                ),
            )
            if wake_epoch is not None:
                trace["outcome"] = "waiting_after_home"
                await self._settle_after_home(generation, wake_epoch, trace)
            self._current(generation)
            trace["outcome"] = "acknowledged"
            self._set_wired_controller_power_estimate(
                "On", "HA wake command acknowledged", generation=generation
            )
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            trace["outcome"] = "failed"
            trace["error"] = str(exc)
            raise
        finally:
            trace["status_after_hex"] = self.status.raw.hex().upper() if self.status else None

    async def _settle_after_home(self, generation: int, wake_epoch: int, trace: dict) -> None:
        """Wait once after an explicit wake; never send or replay a chair command."""
        started = self._clock()
        wall_started = asyncio.get_running_loop().time()
        sequence = self._status_sequence
        settling = {
            "outcome": "waiting",
            "required_seconds": POST_HOME_WAKE_SECONDS,
            "status_reports": 0,
        }
        trace["post_home_settle"] = settling
        try:
            try:
                maximum_wait = POST_HOME_WAKE_SECONDS + self._stale_after
                async with asyncio.timeout(maximum_wait):
                    while True:
                        self._current(generation)
                        if (
                            self._status_sequence > sequence
                            and self._received_at >= started + POST_HOME_WAKE_SECONDS
                        ):
                            break
                        remaining = min(
                            maximum_wait - (self._clock() - started),
                            maximum_wait - (asyncio.get_running_loop().time() - wall_started),
                        )
                        if remaining <= 0:
                            raise TimeoutError
                        await self._sleep(min(POST_HOME_WAKE_POLL_SECONDS, remaining))
            except TimeoutError:
                raise ControlError(
                    "The chair did not send fresh status after waking from Home; "
                    "the requested control was not sent."
                ) from None
            self._current(generation)
            if wake_epoch != self._home_wake_epoch:
                raise CommandSuperseded
            self._home_wake_pending = False
            settling["outcome"] = "settled"
        except asyncio.CancelledError:
            settling["outcome"] = "cancelled"
            raise
        except Exception:
            settling["outcome"] = "failed"
            raise
        finally:
            settling["duration_seconds"] = round(self._clock() - started, 3)
            settling["status_reports"] = (
                self._status_sequence - sequence if generation == self._generation else None
            )

    async def turn_on(self) -> None:
        async with self._serialized_operation() as generation:
            await self._activate(generation, "turn_on")

    def _observe_home_status(self, status: Status, received_at: float) -> None:
        """Track every Home report, including brief changes between monitor polls."""
        session = self._home_session
        if session is None or session["generation"] != self._generation:
            return
        trace = session["trace"]
        trace["status_reports"] += 1
        raw = status.raw.hex().upper()
        reports = trace["reports"]
        if not reports or reports[-1]["status_hex"] != raw:
            reports.append(
                {
                    "seconds": round(received_at - session["started"], 3),
                    "positions": list(status.positions),
                    "heat": status.heat,
                    "massage": status.massage,
                    "status_hex": raw,
                }
            )
            del reports[:-64]
        if not self._home_positions_valid(status):
            session["invalid_positions"] = True
        neutral = (
            status.positions == (0, 0, 0, 0) and status.heat is False and status.massage == "Off"
        )
        if not neutral:
            session["neutral_since"] = None
            session["neutral_reports"] = 0
        elif (
            session["neutral_since"] is None
            or received_at - session["last_report_at"] > self._stale_after
        ):
            session["neutral_since"] = received_at
            session["neutral_reports"] = 1
        else:
            session["neutral_reports"] += 1
        session["last_report_at"] = received_at
        trace["neutral_stable_seconds"] = (
            round(received_at - session["neutral_since"], 3)
            if session["neutral_since"] is not None
            else 0.0
        )

    @staticmethod
    def _home_positions_valid(status: Status) -> bool:
        return all(
            lower <= position <= upper
            for position, (lower, upper) in zip(status.positions, OBSERVED_BOUNDS, strict=True)
        )

    async def return_home(self) -> None:
        """Send native Home once and observe stable neutral motor/heat/massage feedback.

        Positions do not verify lumbar pressure or controller power. Successful
        completion separately records a command-based controller-power estimate.
        Native travel can be staged and briefly move away from zero.
        """
        if self._home_completion is not None:
            # A repeated Home replaces the pending follow-up, not the native
            # cycle already in progress. Do not dispatch Home a second time.
            await self._wait_for_home(self._reserve_operation())
            return
        trace = {
            "outcome": "pending",
            "command_hex": HOME_COMMAND.hex().upper(),
            "status_reports": 0,
            "neutral_stable_seconds": 0.0,
            "reports": [],
        }
        self.last_home_request = trace
        requested_at = self._clock()
        session = None
        try:
            async with self._operation(native_home=True) as generation:
                status = self._current(generation)
                if not self._home_positions_valid(status):
                    raise ControlError("Current position is outside the observed range.")
                trace["status_before_hex"] = status.raw.hex().upper()
                trace["positions_before"] = list(status.positions)
                # Retire renewals at dispatch, preserving the remembered choices
                # just as an explicit Heat/Massage Off does. No new session starts.
                self._stop_massage_session("home_requested")
                self._stop_heat_session("home_requested")
                started = self._clock()
                session = {
                    "generation": generation,
                    "started": started,
                    "trace": trace,
                    "last_report_at": float("-inf"),
                    "neutral_since": None,
                    "neutral_reports": 0,
                    "invalid_positions": False,
                }
                self._home_session = session
                # Delivery may succeed even if the transport later raises.
                self._home_wake_epoch += 1
                self._home_wake_pending = True
                self._needs_stop = True
                self._set_wired_controller_power_estimate(
                    None,
                    "HA Return to neutral command in progress or incomplete",
                    generation=generation,
                )
                try:
                    async with asyncio.timeout(HOME_MAX_SECONDS):
                        await self._write_packet(HOME_COMMAND)
                        # Home can match the pre-command state. Reports while a
                        # transport write is pending do not prove it was sent;
                        # start the full observation window after it returns.
                        # Keep any invalid bounds seen during that write.
                        session["neutral_since"] = None
                        session["neutral_reports"] = 0
                        session["last_report_at"] = float("-inf")
                        trace["neutral_stable_seconds"] = 0.0
                        trace["outcome"] = "waiting_for_reported_neutral"
                        while True:
                            self._current(generation)
                            if session["invalid_positions"]:
                                raise ControlError(
                                    "Unexpected Home position feedback; a movement stop was attempted."
                                )
                            if self._clock() - started >= HOME_MAX_SECONDS:
                                raise TimeoutError
                            if (
                                session["neutral_reports"] >= 2
                                and session["last_report_at"] - session["neutral_since"]
                                >= HOME_STABLE_SECONDS
                            ):
                                break
                            await self._sleep(HOME_POLL_SECONDS)
                except BaseException as exc:
                    await self._best_effort_release(generation, motors=(0x80,) * 4)
                    if isinstance(exc, TimeoutError):
                        raise ControlError(
                            "Home did not report stable neutral positions with heat and massage Off "
                            "within the time limit; a movement stop was attempted."
                        ) from exc
                    raise
                self._needs_stop = False
                trace["outcome"] = "reported_neutral"
                self._set_wired_controller_power_estimate(
                    "Off", "HA Return to neutral completed", generation=generation
                )
        except CommandSuperseded:
            trace["outcome"] = "replaced_by_new_request"
            raise
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            trace["outcome"] = "failed"
            trace["error"] = str(exc)
            raise
        finally:
            if self._home_session is session:
                self._home_session = None
            trace["duration_seconds"] = round(self._clock() - requested_at, 3)
            trace["positions_after"] = list(self.status.positions) if self.status else None
            trace["status_after_hex"] = self.status.raw.hex().upper() if self.status else None

    async def select_preset(self, name: str) -> None:
        """Select a captured native program once; firmware owns its travel.

        A new matching reply confirms selection, not completion of movement.
        The next control sends a stop before taking over, including after this
        short request has returned. Nothing is replayed on reconnection.
        """
        try:
            payload = preset_command(name)
        except ValueError as exc:
            raise ControlError(str(exc)) from exc
        trace = {"preset": name, "outcome": "pending"}
        self.preset_requests.append(trace)
        try:
            async with self._operation() as generation:
                await self._activate(generation, "preset")
                trace["status_before_hex"] = self._current(generation).raw.hex().upper()
                trace["command_hex"] = payload.hex().upper()
                sequence = self._status_sequence
                # A transport error can happen after the chair received a write.
                # Keep a stop pending until the next successful handoff as well.
                self._needs_stop = True
                try:
                    # An unsent preset cannot replace the running timers. Once the
                    # write starts, delivery may be uncertain even if it raises.
                    self._stop_massage_session("preset_selected", clear_selection=True)
                    self._stop_heat_session("preset_selected", clear_selection=True)
                    await self._write_packet(payload)
                    trace["outcome"] = "waiting_for_status"
                    await self._wait_for(
                        generation,
                        lambda status: (
                            self._status_sequence > sequence
                            and status.raw[6:13] == payload[6:13]
                            and status.raw[17:25] == payload[17:25]
                            and all(
                                lower <= position <= upper
                                for position, (lower, upper) in zip(
                                    status.positions, OBSERVED_BOUNDS, strict=True
                                )
                            )
                        ),
                        timeout_message=(
                            "The chair did not confirm preset selection; a movement stop was attempted."
                        ),
                    )
                except BaseException:
                    await self._best_effort_release(generation, motors=(0x80,) * 4)
                    raise
                trace["outcome"] = "accepted"
                # Discard the pre-selection stationary window. Later fresh,
                # unchanged positions can establish that this program settled.
                self._reset_position_stability()
        except CommandSuperseded:
            trace["outcome"] = "replaced_by_new_request"
            raise
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            trace["outcome"] = "failed"
            trace["error"] = str(exc)
            raise
        finally:
            trace["status_after_hex"] = self.status.raw.hex().upper() if self.status else None

    async def _release(self, generation: int, *, motors=(0x8F,) * 4) -> None:
        try:
            await self._send(generation, motors=motors, lumbar=0)
        except BaseException:
            # Delivery is uncertain. Require the existing stop handoff on the
            # next request, but never carry a release across reconnection.
            if generation == self._generation:
                self._needs_stop = True
            raise

    async def _best_effort_release(self, generation: int, *, motors=(0x8F,) * 4) -> None:
        try:
            await self._release(generation, motors=motors)
        except Exception:
            _LOGGER.warning("Could not send the release with fresh state; no command was replayed")

    @staticmethod
    def _memory_positions(status: Status) -> tuple[int, ...]:
        positions = status.positions
        if any(
            not lower <= position <= upper
            for position, (lower, upper) in zip(positions, OBSERVED_BOUNDS, strict=True)
        ):
            raise ControlError("Unexpected memory position feedback; movement stopped.")
        return positions

    def reserve_memory_recall(self) -> _OperationRequest:
        """Order a recall before storage I/O without touching movement or timers.

        A later request, Stop or reconnect invalidates this reservation. Two
        pending recalls are ordered here, rather than by storage completion.
        """
        return self._reserve_operation()

    async def recall_memory(
        self, slot: int, profile: MemoryProfile, *, request: _OperationRequest | None = None
    ) -> None:
        """Apply one saved full-state target and monitor its reported completion.

        This adapts the captured remote memory messages to HA-owned profiles.
        Physical recall from arbitrary positions still needs on-chair validation.
        """
        if request is not None and (request.generation, request.intent_sequence) != (
            self._generation,
            self._intent_sequence,
        ):
            raise CommandSuperseded
        if type(slot) is not int or slot not in (1, 2, 3):
            raise ControlError("Choose Memory 1, 2 or 3.")
        if not isinstance(profile, MemoryProfile):
            raise ControlError("This memory has no valid saved profile.")
        current = self._current(self._generation)
        self._memory_positions(current)
        try:
            # Reject invalid profiles before interrupting anything or activating.
            profile.command(current, slot)
        except ValueError as exc:
            raise ControlError(str(exc)) from exc
        trace = {
            "slot": slot,
            "target_positions": list(profile.positions),
            "outcome": "pending",
            "reports": [],
        }
        self.memory_requests.append(trace)
        requested_at = self._clock()
        try:
            async with self._operation(request=request) as generation:
                recall_intent = self._request_id, self._intent_sequence
                self._memory_positions(self._current(generation))
                await self._activate(generation, "memory")
                current = self._current(generation)
                self._memory_positions(current)
                trace["status_before_hex"] = current.raw.hex().upper()
                try:
                    payload = profile.command(current, slot)
                except ValueError as exc:
                    raise ControlError(str(exc)) from exc
                if recall_intent != (self._request_id, self._intent_sequence):
                    raise CommandSuperseded
                # Only this write can replace the previous timers. Retire
                # their workers before delivery becomes uncertain; publish the
                # new choices together after the chair reaches the target.
                self._stop_massage_session("memory_selected")
                self._stop_heat_session("memory_selected")
                trace["command_hex"] = payload.hex().upper()
                sequence = self._status_sequence
                started = self._clock()
                self._reset_position_stability()
                self._needs_stop = True
                try:
                    async with asyncio.timeout(MEMORY_MAX_SECONDS):
                        # Send the absolute target only once; firmware owns travel.
                        await self._write_packet(payload)
                        trace["outcome"] = "waiting_for_status"

                        def acknowledged(status: Status) -> bool:
                            self._memory_positions(status)
                            return (
                                self._status_sequence > sequence
                                and status.raw[10] == 0x10 + slot
                                and profile.matches_settings(status)
                            )

                        await self._wait_for(
                            generation,
                            acknowledged,
                            timeout_message=(
                                "The chair did not confirm memory recall; a movement stop was attempted."
                            ),
                        )
                        trace["outcome"] = "moving"
                        await self._monitor_memory(
                            generation, slot, profile, trace, requested_at, started
                        )
                except BaseException as exc:
                    await self._best_effort_release(generation, motors=(0x80,) * 4)
                    if isinstance(exc, TimeoutError):
                        raise ControlError(
                            "Memory recall time limit reached; movement stopped."
                        ) from exc
                    raise
                self._needs_stop = False
                await self._restore_memory_timers(generation, profile, started, recall_intent)
                trace["outcome"] = "target_reached"
        except CommandSuperseded:
            trace["outcome"] = "replaced_by_new_request"
            raise
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            trace["outcome"] = "failed"
            trace["error"] = str(exc)
            raise
        finally:
            trace["duration_seconds"] = round(self._clock() - requested_at, 3)
            trace["status_after_hex"] = self.status.raw.hex().upper() if self.status else None
            self._record_position_report(trace, requested_at)

    async def _restore_memory_timers(
        self,
        generation: int,
        profile: MemoryProfile,
        command_started: float,
        recall_intent: tuple[int, int],
    ) -> None:
        """Manage the timers already started by a successful recall, without resending it."""
        if recall_intent != (self._request_id, self._intent_sequence):
            raise CommandSuperseded
        status = self._current(generation)
        if not profile.matches_settings(status) or status.positions != profile.positions:
            raise ControlError("Chair settings changed before memory timers could be restored.")
        selector = status.raw[10]
        massage_session, heat_session = self._massage_session, self._heat_session
        self._massage_continuous = profile.massage_continuous
        self._heat_continuous = profile.heat_continuous
        # Protect this asynchronous disk commit like an ordinary pending On.
        # An observed Off must invalidate the session even if a later passive
        # On restores identical fields before the commit finishes.
        self._massage_starting = profile.massage_continuous and profile.massage != "Off"
        self._massage_starting_active = self._massage_starting
        self._massage_starting_timer = status.raw[6:8] if self._massage_starting else None
        self._massage_selector = selector
        self._heat_starting = profile.heat_continuous and profile.heat
        self._heat_starting_active = self._heat_starting
        self._heat_selector = selector
        self._heat_expected_timer = status.raw[8:10]
        self._heat_pending_timer = None
        self._massage_renewal_trace = {
            "outcome": "starting"
            if self._massage_starting
            else "armed"
            if profile.massage_continuous
            else "memory_selected",
            "renewal_count": 0,
        }
        self._heat_renewal_trace = {
            "outcome": "starting"
            if self._heat_starting
            else "armed"
            if profile.heat_continuous
            else "memory_selected",
            "renewal_count": 0,
        }
        self._preferences_changed(self.timer_preferences)
        self._changed()
        try:
            await self._flush_timer_preferences()
            if (
                recall_intent != (self._request_id, self._intent_sequence)
                or massage_session != self._massage_session
                or heat_session != self._heat_session
            ):
                raise CommandSuperseded
            status = self._current(generation)
            if (
                status.raw[10] != selector
                or not profile.matches_settings(status)
                or status.positions != profile.positions
            ):
                raise ControlError("Chair settings changed before memory timers could be restored.")
            elapsed = max(0.0, self._clock() - command_started)
            if profile.massage_continuous and profile.massage != "Off":
                self._massage_running = True
                self._massage_starting = False
                self._massage_starting_timer = None
                self._massage_selector = status.raw[10]
                self._massage_renewal_trace["outcome"] = "running"
                self._massage_renewal_task = asyncio.create_task(
                    self._massage_renewal_loop(
                        generation,
                        massage_session,
                        initial_delay=max(0.0, MASSAGE_RENEW_SECONDS - elapsed),
                    ),
                    name="svago_massage_renewal",
                )
                self._massage_tasks.add(self._massage_renewal_task)
                self._massage_renewal_task.add_done_callback(self._massage_tasks.discard)
            if profile.heat_continuous and profile.heat:
                self._heat_running = True
                self._heat_starting = False
                self._heat_selector = status.raw[10]
                self._heat_expected_timer = status.raw[8:10]
                self._heat_pending_timer = None
                self._heat_renewal_trace["outcome"] = "running"
                # The recall already set 180 minutes, even if a finite massage
                # currently prevents a future automatic renewal. Wait until the
                # renewal is due; do not issue an immediate extra heat command.
                self._heat_renewal_task = asyncio.create_task(
                    self._heat_renewal_loop(
                        generation,
                        heat_session,
                        initial_delay=max(0.0, HEAT_RENEW_SECONDS - elapsed),
                    ),
                    name="svago_heat_renewal",
                )
                self._heat_tasks.add(self._heat_renewal_task)
                self._heat_renewal_task.add_done_callback(self._heat_tasks.discard)
            self._changed()
        except BaseException:
            # A replacement may already own new choices or workers. Never
            # cancel those when unwinding this older memory request.
            if massage_session == self._massage_session and profile.massage_continuous:
                self._stop_massage_session("start_failed")
                self._massage_renewal_trace["outcome"] = "start_failed"
            if heat_session == self._heat_session and profile.heat_continuous:
                self._stop_heat_session("start_failed")
                self._heat_renewal_trace["outcome"] = "start_failed"
            self._changed()
            raise

    async def _monitor_memory(self, generation, slot, profile, trace, requested_at, started):
        positions = self._memory_positions(self._current(generation))
        best_remaining = sum(
            abs(position - target)
            for position, target in zip(positions, profile.positions, strict=True)
        )
        last_progress = self._clock()
        while True:
            status = self._current(generation)
            positions = self._memory_positions(status)
            self._record_position_report(trace, requested_at)
            if status.raw[10] != 0x10 + slot or not profile.matches_settings(status):
                raise ControlError("Chair settings changed during memory recall; movement stopped.")
            if positions == profile.positions:
                return
            now = self._clock()
            if now - started >= MEMORY_MAX_SECONDS:
                raise ControlError("Memory recall time limit reached; movement stopped.")
            remaining = sum(
                abs(position - target)
                for position, target in zip(positions, profile.positions, strict=True)
            )
            if remaining < best_remaining:
                # Allow brief coupled/overshooting movement on one axis while
                # requiring net progress across all axes. Jitter and sustained
                # wrong-direction travel cannot keep the watchdog alive.
                last_progress = now
                best_remaining = remaining
            if now - last_progress >= MEMORY_STALL_SECONDS:
                raise ControlError(
                    "Memory position stopped changing before the target; movement stopped."
                )
            await self._sleep(MEMORY_POLL_SECONDS)

    async def _wait_for(
        self, generation: int, predicate: Callable[[Status], bool], *, timeout_message=None
    ) -> None:
        try:
            async with asyncio.timeout(self._feedback_timeout):
                while True:
                    status = self._current(generation)
                    if predicate(status):
                        return
                    self._updated.clear()
                    # Also re-check freshness when notifications stop altogether.
                    try:
                        await asyncio.wait_for(self._updated.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
        except TimeoutError as exc:
            raise ControlError(
                timeout_message
                or "The chair did not confirm the requested setting; it was not resent."
            ) from exc

    async def set_heat(self, enabled: bool) -> None:
        if not enabled:
            self._stop_heat_session("turned_off")
        trace = {"requested": enabled, "outcome": "pending"}
        self.last_heat_request = trace
        try:
            operation = self._serialized_operation() if enabled else self._operation()
            async with operation as generation:
                continuous = enabled and self._heat_continuous
                if continuous:
                    self._stop_heat_session("restarted")
                    session = self._heat_session
                if enabled:
                    await self._activate(generation, "heat_on")
                status = self._current(generation)
                sequence = self._status_sequence
                trace["status_before_hex"] = status.raw.hex().upper()
                if continuous:
                    if not self._can_renew_heat(status):
                        # An explicit Heat On keeps its existing behavior and
                        # current duration. Do not add a second write resetting
                        # a finite massage just to establish Continuous heat.
                        payload = status.command(motors=(0xFF,) * 4, heat=True)
                        trace["command_hex"] = payload.hex().upper()
                        await self._write_packet(payload)
                        await self._wait_for(
                            generation,
                            lambda status: self._status_sequence > sequence and status.heat is True,
                        )
                    else:
                        trace["command_hex"] = status.continuous_heat_command().hex().upper()
                    await self._begin_continuous_heat(generation, session)
                else:
                    trace["command_hex"] = (
                        status.command(motors=(0xFF,) * 4, heat=enabled).hex().upper()
                    )
                    await self._send(generation, motors=(0xFF,) * 4, heat=enabled)
                    trace["outcome"] = "waiting_for_status"
                    await self._wait_for(
                        generation,
                        lambda status: self._status_sequence > sequence and status.heat is enabled,
                    )
                trace["outcome"] = "confirmed"
        except CommandSuperseded:
            trace["outcome"] = "replaced_by_new_request"
            raise
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            trace["outcome"] = "failed"
            trace["error"] = str(exc)
            raise
        finally:
            trace["status_after_hex"] = self.status.raw.hex().upper() if self.status else None

    async def set_massage(self, mode: str) -> None:
        if mode not in MASSAGE_MODES:
            raise ControlError("Unknown massage mode")
        if mode == "Off":
            self._stop_massage_session("turned_off")
        operation = self._serialized_operation() if mode != "Off" else self._operation()
        async with operation as generation:
            continuous = mode != "Off" and self._massage_continuous
            if continuous:
                self._stop_massage_session("mode_changed")
                session = self._massage_session
            if mode != "Off":
                await self._activate(generation, "massage")
            if continuous:
                await self._begin_continuous_massage(generation, session, mode)
            else:
                sequence = self._status_sequence
                await self._send(generation, motors=(0xFF,) * 4, massage=mode)
                await self._wait_for(
                    generation,
                    lambda status: self._status_sequence > sequence and status.massage == mode,
                )

    async def set_massage_continuous(self) -> None:
        """Select Continuous, arming only an explicit future start when Off."""
        self._current(self._generation)
        self._stop_massage_session("selected")
        self._massage_continuous = True
        self._preferences_changed(self.timer_preferences)
        session = self._massage_session
        self._changed()
        async with self._serialized_operation() as generation:
            if not self._massage_continuous or session != self._massage_session:
                return
            await self._flush_timer_preferences()
            if not self._massage_continuous or session != self._massage_session:
                return
            status = self._current(generation)
            if status.massage == "Off":
                self._massage_renewal_trace = {"outcome": "armed", "renewal_count": 0}
                return
            await self._begin_continuous_massage(generation, session, status.massage)

    async def _begin_continuous_massage(self, generation: int, session: int, mode: str) -> None:
        if not self._massage_continuous or session != self._massage_session:
            return
        self._massage_renewal_trace = {"outcome": "starting", "renewal_count": 0}
        try:
            status = self._current(generation)
            self._massage_starting = True
            self._massage_starting_active = status.massage not in (None, "Off")
            self._massage_starting_timer = status.raw[6:8]
            self._massage_selector = status.raw[10]
            await self._write_continuous_massage(generation, mode)
            if not self._massage_continuous or session != self._massage_session:
                return
            self._massage_running = True
            self._massage_starting = False
            self._massage_starting_timer = None
            self._massage_selector = self._current(generation).raw[10]
            self._massage_renewal_trace["outcome"] = "running"
            self._massage_renewal_task = asyncio.create_task(
                self._massage_renewal_loop(generation, session), name="svago_massage_renewal"
            )
            self._massage_tasks.add(self._massage_renewal_task)
            self._massage_renewal_task.add_done_callback(self._massage_tasks.discard)
            self._changed()
        except BaseException:
            if session == self._massage_session:
                self._stop_massage_session("start_failed")
                self._massage_renewal_trace["outcome"] = "start_failed"
                self._changed()
            raise

    async def _write_continuous_massage(self, generation: int, mode: str) -> None:
        payload = self._current(generation).continuous_massage_command(mode)
        self._massage_renewal_trace["command_hex"] = payload.hex().upper()
        sequence = self._status_sequence
        await self._write_packet(payload)
        await self._wait_for(
            generation,
            lambda status: (
                self._status_sequence > sequence
                and status.massage == mode
                and status.timer_minutes("massage") == CONTINUOUS_MASSAGE_MINUTES
            ),
        )

    async def _renew_massage_once(self, generation: int, session: int) -> bool:
        """Take idle write access without cancelling an in-progress chair action."""
        if self.busy or self._settings_lock.locked() or self._needs_stop:
            return False
        async with self._settings_lock:
            if self.busy or self._needs_stop:
                return False
            async with self._transition:
                if not self._massage_session_valid(generation, session):
                    return False
                status = self._current(generation)
                if status.massage in (None, "Off"):
                    self._stop_massage_session("chair_stopped")
                    return False
                if status.raw[18] != 0:
                    return False
                mode = status.massage
                payload = status.continuous_massage_command()
                sequence = self._status_sequence
                self._massage_renewal_trace["command_hex"] = payload.hex().upper()
                self._massage_renewal_trace["outcome"] = "renewing"
                await self._write_packet(payload)
                if not self._massage_session_valid(generation, session):
                    return False
        # Foreground commands may take over immediately; don't keep either lock
        # while waiting for the ordinary notification acknowledging this write.
        await self._wait_for(
            generation,
            lambda status: (
                self._status_sequence > sequence
                and status.massage == mode
                and status.timer_minutes("massage") == CONTINUOUS_MASSAGE_MINUTES
            ),
        )
        if not self._massage_session_valid(generation, session):
            return False
        self._massage_renewal_trace["renewal_count"] += 1
        self._massage_renewal_trace["outcome"] = "renewed"
        return True

    async def _massage_renewal_loop(
        self, generation: int, session: int, *, initial_delay: float | None = None
    ) -> None:
        delay = MASSAGE_RENEW_SECONDS if initial_delay is None else initial_delay
        try:
            while self._massage_session_valid(generation, session):
                await asyncio.sleep(delay)
                if not self._massage_session_valid(generation, session):
                    return
                self._current(generation)
                renewed = await self._renew_massage_once(generation, session)
                delay = MASSAGE_RENEW_SECONDS if renewed else MASSAGE_DEFER_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if session == self._massage_session:
                self._massage_renewal_trace["error"] = str(exc)
                self._stop_massage_session("renewal_failed")
                _LOGGER.warning("Continuous massage renewal stopped: %s", exc)
        finally:
            if self._massage_renewal_task is asyncio.current_task():
                self._massage_renewal_task = None
                self._massage_running = False
                self._changed()

    async def set_heat_continuous(self) -> None:
        """Select Continuous without turning on an idle heater."""
        self._current(self._generation)
        self._stop_heat_session("selected")
        self._heat_continuous = True
        self._preferences_changed(self.timer_preferences)
        session = self._heat_session
        self._changed()
        async with self._serialized_operation() as generation:
            if not self._heat_continuous or session != self._heat_session:
                return
            await self._flush_timer_preferences()
            if not self._heat_continuous or session != self._heat_session:
                return
            if self._current(generation).heat is not True:
                self._heat_renewal_trace = {"outcome": "armed", "renewal_count": 0}
                return
            await self._begin_continuous_heat(generation, session)

    def _can_renew_heat(self, status: Status) -> bool:
        """Never restart a finite/unmanaged massage through an automatic heat write."""
        return status.massage == "Off" or (
            status.massage in MASSAGE_MODES[1:]
            and self._massage_session_valid(self._generation, self._massage_session)
            and status.timer_minutes("massage") == CONTINUOUS_MASSAGE_MINUTES
        )

    async def _begin_continuous_heat(self, generation: int, session: int) -> None:
        if not self._heat_continuous or session != self._heat_session:
            return
        self._heat_renewal_trace = {"outcome": "starting", "renewal_count": 0}
        try:
            status = self._current(generation)
            deferred = not self._can_renew_heat(status)
            if not deferred:
                payload = status.continuous_heat_command()
                self._heat_starting = True
                self._heat_starting_active = status.heat is True
                self._heat_selector = status.raw[10]
                self._heat_expected_timer = status.raw[8:10]
                self._heat_pending_timer = payload[8:10]
                self._heat_renewal_trace["command_hex"] = payload.hex().upper()
                sequence = self._status_sequence
                await self._write_packet(payload)
                await self._wait_for(
                    generation,
                    lambda status: (
                        self._status_sequence > sequence
                        and status.heat is True
                        and status.timer_minutes("heat") == CONTINUOUS_HEAT_MINUTES
                    ),
                )
            if not self._heat_continuous or session != self._heat_session:
                return
            status = self._current(generation)
            if status.heat is not True:
                self._stop_heat_session("chair_stopped")
                return
            self._heat_running = True
            self._heat_starting = False
            self._heat_selector = status.raw[10]
            self._heat_expected_timer = status.raw[8:10]
            self._heat_pending_timer = None
            self._heat_renewal_trace["outcome"] = "deferred_massage" if deferred else "running"
            self._heat_renewal_task = asyncio.create_task(
                self._heat_renewal_loop(generation, session, deferred=deferred),
                name="svago_heat_renewal",
            )
            self._heat_tasks.add(self._heat_renewal_task)
            self._heat_renewal_task.add_done_callback(self._heat_tasks.discard)
            self._changed()
        except BaseException:
            if session == self._heat_session:
                self._stop_heat_session("start_failed")
                self._heat_renewal_trace["outcome"] = "start_failed"
                self._changed()
            raise

    async def _renew_heat_once(self, generation: int, session: int) -> bool:
        """Renew idle fresh state without activation or taking over user controls."""
        if self.busy or self._settings_lock.locked() or self._needs_stop:
            return False
        async with self._settings_lock:
            if self.busy or self._needs_stop:
                return False
            async with self._transition:
                if not self._heat_session_valid(generation, session):
                    return False
                status = self._current(generation)
                if status.heat is not True:
                    self._stop_heat_session("chair_stopped")
                    return False
                if status.raw[18] != 0:
                    return False
                if not self._can_renew_heat(status):
                    self._heat_renewal_trace["outcome"] = "deferred_massage"
                    return False
                payload = status.continuous_heat_command()
                sequence = self._status_sequence
                self._heat_renewal_trace["command_hex"] = payload.hex().upper()
                self._heat_renewal_trace["outcome"] = "renewing"
                self._heat_pending_timer = payload[8:10]
                await self._write_packet(payload)
                if not self._heat_session_valid(generation, session):
                    return False
        # Neither the other renewal worker nor a foreground action waits on
        # this confirmation. Off can cancel only this session immediately.
        await self._wait_for(
            generation,
            lambda status: (
                self._status_sequence > sequence
                and status.heat is True
                and status.timer_minutes("heat") == CONTINUOUS_HEAT_MINUTES
            ),
        )
        if not self._heat_session_valid(generation, session):
            return False
        self._heat_expected_timer = self._heat_pending_timer
        self._heat_pending_timer = None
        self._heat_renewal_trace["renewal_count"] += 1
        self._heat_renewal_trace["outcome"] = "renewed"
        return True

    async def _heat_renewal_loop(
        self,
        generation: int,
        session: int,
        *,
        deferred: bool = False,
        initial_delay: float | None = None,
    ) -> None:
        delay = (
            initial_delay
            if initial_delay is not None
            else HEAT_DEFER_SECONDS
            if deferred
            else HEAT_RENEW_SECONDS
        )
        try:
            while self._heat_session_valid(generation, session):
                await asyncio.sleep(delay)
                if not self._heat_session_valid(generation, session):
                    return
                self._current(generation)
                renewed = await self._renew_heat_once(generation, session)
                delay = HEAT_RENEW_SECONDS if renewed else HEAT_DEFER_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if session == self._heat_session:
                self._heat_renewal_trace["error"] = str(exc)
                self._stop_heat_session("renewal_failed")
                _LOGGER.warning("Continuous heat renewal stopped: %s", exc)
        finally:
            if self._heat_renewal_task is asyncio.current_task():
                self._heat_renewal_task = None
                self._heat_running = False
                self._changed()

    async def set_heat_level(self, zone: str, level: str) -> None:
        if zone not in HEAT_ZONES or level not in HEAT_LEVELS:
            raise ControlError("Choose a valid heat zone and level.")
        await self._set_setting(
            f"{zone}_heat",
            level,
            {"zone": zone, "level": level},
            lambda status: status.heat_level(zone) == level,
        )

    async def set_timer(self, kind: str, minutes: int) -> None:
        if (
            not isinstance(kind, str)
            or kind not in TIMER_OPTIONS
            or type(minutes) is not int
            or minutes not in TIMER_OPTIONS[kind]
        ):
            raise ControlError("Choose one of the supported timer durations.")
        if kind == "massage":
            self._stop_massage_session("finite_timer_selected", clear_selection=True)
        else:
            self._stop_heat_session("finite_timer_selected", clear_selection=True)
        await self._set_setting(
            f"{kind}_timer",
            minutes,
            {"timer": kind, "minutes": minutes},
            lambda status: status.timer_minutes(kind) == minutes,
        )
        await self._flush_timer_preferences()

    async def _set_setting(self, name, requested, changes, matches) -> None:
        """Apply one setting and require new feedback without turning a function on."""
        trace = {"setting": name, "requested": requested, "outcome": "pending"}
        self.setting_requests.append(trace)
        try:
            async with self._serialized_operation() as generation:
                status = self._current(generation)
                trace["status_before_hex"] = status.raw.hex().upper()
                try:
                    payload = status.setting_command(**changes)
                except ValueError as exc:
                    raise ControlError(str(exc)) from exc
                trace["command_hex"] = payload.hex().upper()
                sequence = self._status_sequence
                await self._write_packet(payload)
                trace["outcome"] = "waiting_for_status"
                await self._wait_for(
                    generation,
                    lambda status: self._status_sequence > sequence and matches(status),
                )
                trace["outcome"] = "confirmed"
        except CommandSuperseded:
            trace["outcome"] = "replaced_by_new_request"
            raise
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            trace["outcome"] = "failed"
            trace["error"] = str(exc)
            raise
        finally:
            trace["status_after_hex"] = self.status.raw.hex().upper() if self.status else None

    async def adjust_lumbar(self, inflate: bool) -> None:
        async with self._operation() as generation:
            await self._activate(generation, "lumbar")
            deadline = self._clock() + LUMBAR_BURST_SECONDS
            writing = False
            try:
                async with asyncio.timeout(LUMBAR_BURST_SECONDS):
                    for _ in range(ceil(LUMBAR_BURST_SECONDS / MOTOR_REPEAT_SECONDS)):
                        if self._clock() >= deadline:
                            break
                        writing = True
                        await self._send(generation, lumbar=2 if inflate else 1)
                        writing = False
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            break
                        await self._sleep(min(MOTOR_REPEAT_SECONDS, remaining))
            except TimeoutError:
                # Expiring during the final sleep is normal. An in-flight
                # write has uncertain delivery and must not be reported as OK.
                if writing:
                    await self._best_effort_release(generation)
                    raise ControlError("Lumbar write timed out; a release was attempted.") from None
            except BaseException:
                await self._best_effort_release(generation)
                raise
            await self._release(generation)

    async def step_position(self, index: int, direction: int) -> str | None:
        """Start or extend one bounded gesture, ending at the observed bound."""
        if index not in range(4) or direction not in (-1, 1):
            raise ControlError("Invalid position step")
        pulse_seconds = MOTOR_PULSE_SECONDS[index]
        trace = {
            "control": POSITION_NAMES[index],
            "direction": direction,
            "requested_pulse_seconds": pulse_seconds,
            "outcome": "pending",
            "direction_writes": 0,
            "direction_write_log": [],
            "stop": {"outcome": "not_attempted"},
            "positions_before": None,
            "feedback_changed": None,
        }
        self.tap_requests.append(trace)
        requested_at = self._clock()
        generation = self._generation
        sequence = self._status_sequence
        try:
            session = self._joinable_tap(index, direction)
            if session is not None:
                result = await self._join_tap(session, trace, pulse_seconds)
            else:
                result = await self._step_position(
                    index, direction, trace, requested_at, pulse_seconds
                )
            # Completion confirms only the bounded pulse and release were sent.
            # Quantized feedback does not prove that a short press moved the chair.
            trace["outcome"] = result or "pulse_completed"
            return result
        except CommandSuperseded:
            trace["outcome"] = "replaced_by_new_request"
            raise
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            trace["outcome"] = "failed"
            trace["error"] = str(exc)
            raise
        finally:
            trace["duration_seconds"] = round(self._clock() - requested_at, 3)
            # Never compare positions from different Bluetooth connections.
            after = self.status if generation == self._generation else None
            trace["positions_after"] = list(after.positions) if after else None
            trace["status_reports"] = (
                self._status_sequence - sequence if generation == self._generation else None
            )
            before = trace["positions_before"]
            if before is not None and after is not None:
                trace["feedback_changed"] = before[index] != after.positions[index]

    def _joinable_tap(self, index: int, direction: int) -> dict | None:
        session = self._tap_session
        if (
            session is None
            or not session["accepting"]
            or session.get("feedback_error") is not None
            or session["index"] != index
            or session["direction"] != direction
            or session["generation"] != self._generation
            or session["request_id"] != self._request_id
            or self._active is not session["owner"]
            or session["owner"] is asyncio.current_task()
            or session["owner"].cancelling()
            or session["done"].done()
        ):
            return None
        if session["phase"] == "moving" and (
            self._clock() >= self._tap_deadline(session)
            or asyncio.get_running_loop().time() >= self._tap_deadline(session, wall=True)
        ):
            return None
        return session

    @staticmethod
    def _tap_deadline(session: dict, *, wall: bool = False) -> float:
        """Timed taps use their pulse deadline; other taps have a startup limit."""
        prefix = "wall_" if wall else ""
        key = (
            "deadline"
            if session["progress_observed"] or not session["require_startup_progress"]
            else "startup_deadline"
        )
        return session[f"{prefix}{key}"]

    def _observe_tap_feedback(self, status: Status, received_at: float) -> None:
        """Keep the normal pulse deadline when feedback arrives during a write."""
        session = self._tap_session
        if (
            session is None
            or session["phase"] != "moving"
            or not session["accepting"]
            or session["generation"] != self._generation
        ):
            return
        trace = session["trace"]
        elapsed = received_at - session["started"]
        try:
            session["best_position"] = _check_movement_feedback(
                status.positions[session["index"]],
                session["start_position"],
                session["best_position"],
                session["direction"],
                OBSERVED_BOUNDS[session["index"]],
                elapsed,
            )
        except ControlError as exc:
            session["feedback_error"] = str(exc)
            trace["startup_outcome"] = "invalid_feedback"
        else:
            if (
                session["best_position"] != session["start_position"]
                and not session["progress_observed"]
            ):
                session["progress_observed"] = True
                trace["first_progress_seconds"] = round(elapsed, 3)
                trace["startup_outcome"] = "progress_observed"
                if session["require_startup_progress"] and (
                    received_at >= session["deadline"]
                    or asyncio.get_running_loop().time() >= session["wall_deadline"]
                ):
                    trace["startup_extended"] = True
        timeout = session.get("timeout")
        if timeout is not None and not timeout.expired():
            timeout.reschedule(
                asyncio.get_running_loop().time()
                if session["feedback_error"]
                else self._tap_deadline(session, wall=True)
            )

    async def _join_tap(self, session: dict, trace: dict, pulse_seconds: float) -> str | None:
        status = self._current(session["generation"])
        self._settings_epoch += 1
        self._intent_sequence += 1
        trace["positions_before"] = list(status.positions)
        trace["coalesced_into_burst"] = session["burst_id"]
        session["trace"]["coalesced_presses"] += 1
        if session["phase"] == "moving":
            session["deadline"] = min(
                session["hard_deadline"], max(session["deadline"], self._clock() + pulse_seconds)
            )
            session["wall_deadline"] = min(
                session["wall_hard_deadline"],
                max(session["wall_deadline"], asyncio.get_running_loop().time() + pulse_seconds),
            )
            session["timeout"].reschedule(self._tap_deadline(session, wall=True))
            session["trace"]["effective_pulse_seconds"] = round(
                session["deadline"] - session["started"], 3
            )
        # During activation, repeated presses join the handshake without
        # building a travel backlog. Cancelling this waiter cannot cancel the
        # shared gesture; Stop/replacement still cancels its owner normally.
        result, error = await asyncio.shield(session["done"])
        # The small request deque can evict the original press during a long
        # gesture. Keep compact totals with each completed joiner so diagnostics
        # still show what was sent without duplicating the packet trace.
        owner_trace = session["trace"]
        trace["shared_burst"] = {
            "direction_writes": owner_trace["direction_writes"],
            "coalesced_presses": owner_trace["coalesced_presses"],
            "effective_pulse_seconds": owner_trace.get("effective_pulse_seconds"),
            "startup_extended": owner_trace.get("startup_extended"),
            "startup_outcome": owner_trace.get("startup_outcome"),
            "first_progress_seconds": owner_trace.get("first_progress_seconds"),
            "stop_outcome": owner_trace["stop"]["outcome"],
        }
        if error is not None:
            raise error
        return result

    async def _trace_tap_write(self, generation, motors, record, requested_at):
        """Record a tap's transport result without adding writes or feedback waits."""
        record["seconds"] = round(self._clock() - requested_at, 3)
        record["write_attempted"] = False
        record["outcome"] = "pending"
        try:
            payload = self._current(generation).command(motors=motors)
            record["command_hex"] = payload.hex().upper()
            record["write_attempted"] = True
            await self._write_packet(payload)
            record["outcome"] = "write_returned"
        except asyncio.CancelledError:
            record["outcome"] = "cancelled"
            raise
        except Exception as exc:
            record["outcome"] = "failed"
            record["error"] = str(exc)
            raise
        finally:
            record["completed_seconds"] = round(self._clock() - requested_at, 3)

    async def _tap_stop(self, generation, trace, requested_at, *, best_effort=False):
        try:
            await self._trace_tap_write(generation, (0x80,) * 4, trace["stop"], requested_at)
        except BaseException as exc:
            if generation == self._generation:
                self._needs_stop = True
            if not best_effort or not isinstance(exc, Exception):
                raise
            _LOGGER.warning("Could not send the release with fresh state; no command was replayed")

    async def _step_position(self, index, direction, trace, requested_at, pulse_seconds):
        session = None
        result = error = None
        try:
            async with self._operation() as generation:
                lower, upper = OBSERVED_BOUNDS[index]

                def checked_position():
                    positions = list(self._current(generation).positions)
                    if any(value >= 0x80 for value in positions):
                        raise ControlError(
                            "Current motor positions are not numeric; wait for fresh status."
                        )
                    if not lower <= positions[index] <= upper:
                        raise ControlError("This step is outside the observed position range.")
                    return positions

                positions = checked_position()
                trace["positions_before"] = positions
                if not lower <= positions[index] + direction <= upper:
                    # Keep a required prior-action handoff, but never activate
                    # or send a direction for an ordinary endpoint no-op.
                    result = "at_observed_limit"
                    return result

                self._tap_sequence += 1
                trace["burst_id"] = self._tap_sequence
                trace["coalesced_presses"] = 0
                session = {
                    "burst_id": self._tap_sequence,
                    "index": index,
                    "direction": direction,
                    "owner": asyncio.current_task(),
                    "generation": generation,
                    "request_id": self._request_id,
                    "phase": "activating",
                    "accepting": True,
                    "trace": trace,
                    "done": asyncio.get_running_loop().create_future(),
                }
                self._tap_session = session
                await self._activate(generation, "movement")
                positions = checked_position()
                trace["positions_at_movement_start"] = positions
                if not lower <= positions[index] + direction <= upper:
                    result = "at_observed_limit"
                    return result

                start = positions[index]
                boundary = upper if direction > 0 else lower
                motors = [0x8F] * 4
                motors[index] = 0x82 if direction > 0 else 0x81
                started = self._clock()
                wall_started = asyncio.get_running_loop().time()
                nominal_seconds = min(pulse_seconds, MOTOR_BURST_MAX_SECONDS)
                session.update(
                    phase="moving",
                    started=started,
                    deadline=started + nominal_seconds,
                    hard_deadline=started + MOTOR_BURST_MAX_SECONDS,
                    startup_deadline=started
                    + min(STARTUP_FEEDBACK_SECONDS, MOTOR_BURST_MAX_SECONDS),
                    wall_deadline=wall_started + nominal_seconds,
                    wall_hard_deadline=wall_started + MOTOR_BURST_MAX_SECONDS,
                    wall_startup_deadline=wall_started
                    + min(STARTUP_FEEDBACK_SECONDS, MOTOR_BURST_MAX_SECONDS),
                    require_startup_progress=index not in (1, 2),
                    progress_observed=False,
                    start_position=start,
                    best_position=start,
                    feedback_error=None,
                )
                trace["effective_pulse_seconds"] = nominal_seconds
                trace["maximum_burst_seconds"] = MOTOR_BURST_MAX_SECONDS
                trace["startup_allowance_seconds"] = (
                    min(STARTUP_FEEDBACK_SECONDS, MOTOR_BURST_MAX_SECONDS)
                    if session["require_startup_progress"]
                    else 0
                )
                trace["startup_extended"] = False
                trace["startup_outcome"] = "pending"
                trace["first_progress_seconds"] = None

                def checked_feedback():
                    status = self._current(generation)
                    self._observe_tap_feedback(status, self._clock())
                    if session["feedback_error"]:
                        raise ControlError(session["feedback_error"])
                    return status.positions[index]

                def pulse_finished():
                    now = self._clock()
                    wall_now = asyncio.get_running_loop().time()
                    nominal_finished = (
                        now >= session["deadline"] or wall_now >= session["wall_deadline"]
                    )
                    if session["progress_observed"] or not session["require_startup_progress"]:
                        return nominal_finished
                    if nominal_finished:
                        trace["startup_extended"] = True
                    if (
                        now >= session["startup_deadline"]
                        or wall_now >= session["wall_startup_deadline"]
                    ):
                        trace["startup_outcome"] = "no_progress"
                        raise ControlError(
                            "The chair did not report movement in the requested direction "
                            "within the startup limit; a stop was attempted."
                        )
                    return False

                try:
                    write_pending = False
                    try:
                        async with asyncio.timeout_at(
                            self._tap_deadline(session, wall=True)
                        ) as timeout:
                            session["timeout"] = timeout
                            # Both clocks and a write-count cap bound a gesture,
                            # even if repeated calls arrive or the transport hangs.
                            for _ in range(ceil(MOTOR_BURST_MAX_SECONDS / MOTOR_REPEAT_SECONDS)):
                                position = checked_feedback()
                                if position == boundary or pulse_finished():
                                    break
                                timeout.reschedule(self._tap_deadline(session, wall=True))
                                write_pending = True
                                record = {}
                                trace["direction_write_log"].append(record)
                                del trace["direction_write_log"][:-8]
                                try:
                                    await self._trace_tap_write(
                                        generation, tuple(motors), record, requested_at
                                    )
                                finally:
                                    trace["direction_writes"] += int(record["write_attempted"])
                                write_pending = False
                                position = checked_feedback()
                                if position == boundary or pulse_finished():
                                    break
                                timeout.reschedule(self._tap_deadline(session, wall=True))
                                repeat_at = self._clock() + MOTOR_REPEAT_SECONDS
                                wall_repeat_at = (
                                    asyncio.get_running_loop().time() + MOTOR_REPEAT_SECONDS
                                )
                                while True:
                                    await self._sleep(
                                        min(
                                            MOTOR_REPEAT_SECONDS
                                            if session["require_startup_progress"]
                                            else max(0, repeat_at - self._clock()),
                                            max(0, self._tap_deadline(session) - self._clock()),
                                            max(
                                                0,
                                                self._tap_deadline(session, wall=True)
                                                - asyncio.get_running_loop().time(),
                                            ),
                                        )
                                    )
                                    if session["require_startup_progress"]:
                                        break
                                    # A clipped sleep or joined press must not
                                    # repeat a short timed tap before the normal
                                    # direction-write interval has elapsed.
                                    position = checked_feedback()
                                    if (
                                        position == boundary
                                        or pulse_finished()
                                        or self._clock() >= repeat_at
                                        or asyncio.get_running_loop().time() >= wall_repeat_at
                                    ):
                                        break
                            else:
                                checked_feedback()
                                if (
                                    session["require_startup_progress"]
                                    and not session["progress_observed"]
                                ):
                                    trace["startup_outcome"] = "no_progress"
                                    raise ControlError(
                                        "The chair did not report movement in the requested direction "
                                        "within the startup limit; a stop was attempted."
                                    )
                    except TimeoutError:
                        if session["feedback_error"]:
                            raise ControlError(session["feedback_error"]) from None
                        if write_pending:
                            trace["direction_write_log"][-1]["outcome"] = "timed_out"
                            if not session["progress_observed"]:
                                trace["startup_outcome"] = "write_timed_out"
                            raise ControlError(
                                "Movement write timed out; a stop was attempted."
                            ) from None
                        # A fresh reply may have arrived just before the timer
                        # expired while sleep was pending. Inspect it once;
                        # never resend a direction after the deadline.
                        checked_feedback()
                        if session["require_startup_progress"] and not session["progress_observed"]:
                            trace["startup_outcome"] = "no_progress"
                            raise ControlError(
                                "The chair did not report movement in the requested direction "
                                "within the startup limit; a stop was attempted."
                            ) from None
                except BaseException:
                    session["accepting"] = False
                    await self._tap_stop(generation, trace, requested_at, best_effort=True)
                    raise
                if not session["require_startup_progress"] and not session["progress_observed"]:
                    trace["startup_outcome"] = "not_observed_during_timed_pulse"
                session["accepting"] = False
                # A returned release is not proof of physical movement.
                await self._tap_stop(generation, trace, requested_at)
        except BaseException as exc:
            error = exc
            raise
        finally:
            if session is not None:
                session["accepting"] = False
                if "started" in session:
                    trace["movement_duration_seconds"] = round(
                        self._clock() - session["started"], 3
                    )
                if self._tap_session is session:
                    self._tap_session = None
                # Store an ordinary result tuple so an unjoined gesture cannot
                # produce an unhandled-Future-exception warning.
                session["done"].set_result((result, error))

    async def set_position(self, index: int, target: int) -> None:
        """Drive continuously toward a slider target using live position feedback."""
        if index not in range(4) or type(target) is not int:
            raise ControlError("Invalid position target")
        lower, upper = OBSERVED_BOUNDS[index]
        if not lower <= target <= upper:
            raise ControlError("Position is outside the observed range.")
        trace = {
            "control": POSITION_NAMES[index],
            "target": target,
            "outcome": "pending",
            "direction_writes": 0,
            "reports": [],
        }
        self.position_requests.append(trace)
        requested_at = self._clock()
        try:
            await self._move_to_position(index, target, trace, requested_at)
            trace["outcome"] = "target_reached"
        except CommandSuperseded:
            trace["outcome"] = "replaced_by_new_request"
            raise
        except asyncio.CancelledError:
            trace["outcome"] = "cancelled"
            raise
        except Exception as exc:
            trace["outcome"] = "failed"
            trace["error"] = str(exc)
            raise
        finally:
            trace["duration_seconds"] = round(self._clock() - requested_at, 3)
            trace["positions_after"] = list(self.status.positions) if self.status else None
            self._record_position_report(trace, requested_at)

    def _record_position_report(self, trace, started):
        if self.status is None:
            return
        positions = list(self.status.positions)
        reports = trace["reports"]
        if not reports or reports[-1]["positions"] != positions:
            reports.append({"seconds": round(self._clock() - started, 3), "positions": positions})
            del reports[:-64]

    async def _move_to_position(self, index, target, trace, requested_at):
        lower, upper = OBSERVED_BOUNDS[index]
        async with self._operation() as generation:
            status = self._current(generation)
            trace["positions_before"] = list(status.positions)
            self._record_position_report(trace, requested_at)
            position = status.positions[index]
            if not lower <= position <= upper:
                raise ControlError("Current position is outside the observed range.")
            if position == target:
                return
            # The wired controller can be in standby when a move is requested.
            # Activate only for a requested move, never during reconnection or
            # for an already-reached target. Use the reply's position below.
            await self._activate(generation, "position")
            status = self._current(generation)
            self._record_position_report(trace, requested_at)
            position = status.positions[index]
            if not lower <= position <= upper:
                raise ControlError("Current position is outside the observed range.")
            if position == target:
                return
            direction = 1 if target > position else -1
            motors = [0x8F] * 4
            motors[index] = 0x82 if direction > 0 else 0x81
            start_position = best = position
            previous_seat = status.positions[2]
            started = last_progress = self._clock()
            attempted = False
            try:
                async with asyncio.timeout(SLIDER_MAX_SECONDS):
                    while True:
                        status = self._current(generation)
                        self._record_position_report(trace, requested_at)
                        position = status.positions[index]
                        now = self._clock()
                        new_best = _check_movement_feedback(
                            position, start_position, best, direction, (lower, upper), now - started
                        )
                        if (position - target) * direction >= 0:
                            break
                        if new_best != best:
                            last_progress = now
                            best = new_best
                        # The real 0.1.6 trace holds backrest=6 while seat moves
                        # 0 -> 1 -> 2. That linked movement is progress too.
                        seat = status.positions[2]
                        if (
                            index == 1
                            and 0 <= seat <= OBSERVED_BOUNDS[2][1]
                            and seat != previous_seat
                        ):
                            previous_seat = seat
                            last_progress = now
                        if now - last_progress >= SLIDER_STALL_SECONDS:
                            raise ControlError(
                                "Position stopped changing before the target; movement stopped."
                            )
                        if now - started >= SLIDER_MAX_SECONDS:
                            raise ControlError("Movement time limit reached; movement stopped.")
                        attempted = True
                        trace["direction_writes"] += 1
                        trace["last_direction_command_hex"] = (
                            status.command(motors=tuple(motors)).hex().upper()
                        )
                        await self._send(generation, motors=tuple(motors))
                        await self._sleep(MOTOR_REPEAT_SECONDS)
            except BaseException as exc:
                if attempted:
                    await self._best_effort_release(generation, motors=(0x80,) * 4)
                if isinstance(exc, TimeoutError):
                    raise ControlError("Movement time limit reached; movement stopped.") from exc
                raise
            await self._release(generation, motors=(0x80,) * 4)

    async def cancel_active(self) -> None:
        self._stop_massage_session("shutdown")
        self._stop_heat_session("shutdown")
        self._settings_epoch += 1
        self._intent_sequence += 1
        # Invalidate foreground waiters before awaiting background cancellation.
        self._request_id += 1
        renewals = (self._massage_tasks | self._heat_tasks) - {asyncio.current_task()}
        for renewal in renewals:
            renewal.cancel()
        if renewals:
            await asyncio.gather(*renewals, return_exceptions=True)
        async with self._transition:
            await self._cancel_active()

    async def _cancel_active(self, *, superseded: bool = False) -> None:
        task = self._active
        if task is not None and task is not asyncio.current_task():
            if superseded:
                self._superseded.add(task)
            task.cancel()
            try:
                # A cancelled replacement must not cancel the old task's
                # release again or continue into its own requested movement.
                await asyncio.shield(task)
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if caller is not None and caller.cancelling():
                    raise
            except Exception:
                pass

    async def stop_movement(self) -> None:
        async with self._operation(interrupt_home=True) as generation:
            await self._send(generation, motors=(0x80,) * 4, lumbar=0)
