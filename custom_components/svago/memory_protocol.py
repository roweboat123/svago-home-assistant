"""Independent HA memory profiles containing only mapped, recallable settings."""

from dataclasses import dataclass

from .protocol import (
    CONTINUOUS_HEAT_MINUTES,
    CONTINUOUS_MASSAGE_MINUTES,
    HEAT_LEVELS,
    HEAT_ZONES,
    MASSAGE_MODES,
    OBSERVED_BOUNDS,
    TIMER_OPTIONS,
    Status,
    checksum,
)


def validate_slot(slot: int) -> None:
    """Reject aliases such as True/1.0 and unimplemented slots."""
    if type(slot) is not int or slot not in (1, 2, 3):
        raise ValueError("Choose Memory 1, 2 or 3")


@dataclass(frozen=True)
class MemoryProfile:
    """An immutable position/settings snapshot, without raw control context."""

    positions: tuple[int, int, int, int]
    massage: str
    heat: bool
    heat_levels: tuple[str, str, str]
    massage_timer_seconds: int
    heat_timer_seconds: int
    massage_continuous: bool = False
    heat_continuous: bool = False

    def __post_init__(self) -> None:
        if type(self.massage_continuous) is not bool or type(self.heat_continuous) is not bool:
            raise ValueError("Memory Continuous choices must be booleans")
        if (
            type(self.positions) is not tuple
            or len(self.positions) != 4
            or any(
                type(value) is not int or not lower <= value <= upper
                for value, (lower, upper) in zip(self.positions, OBSERVED_BOUNDS, strict=True)
            )
        ):
            raise ValueError("Memory positions must be within the observed chair range")
        if type(self.massage) is not str or self.massage not in MASSAGE_MODES:
            raise ValueError("Memory massage mode is unknown")
        if type(self.heat) is not bool:
            raise ValueError("Memory heat state is unknown")
        if (
            type(self.heat_levels) is not tuple
            or len(self.heat_levels) != 3
            or any(type(level) is not str or level not in HEAT_LEVELS for level in self.heat_levels)
        ):
            raise ValueError("Memory heat levels are unknown")
        # Zero heat duration and zero/one massage duration were observed while
        # their functions were Off. Preserve these inactive values only; they
        # must not become invented unlimited or one-second active timers.
        valid_massage = {minutes * 60 for minutes in TIMER_OPTIONS["massage"]}
        if self.massage == "Off":
            valid_massage |= {0, 1}
        valid_heat = {minutes * 60 for minutes in TIMER_OPTIONS["heat"]}
        if not self.heat:
            valid_heat.add(0)
        if (
            type(self.massage_timer_seconds) is not int
            or self.massage_timer_seconds not in valid_massage
        ):
            raise ValueError("Choose a supported massage timer before saving this memory")
        if type(self.heat_timer_seconds) is not int or self.heat_timer_seconds not in valid_heat:
            raise ValueError("Choose a supported heat timer before saving this memory")

    @classmethod
    def from_status(
        cls, status: Status, *, massage_continuous: bool = False, heat_continuous: bool = False
    ) -> "MemoryProfile":
        """Extract mapped values; the controller checks freshness before calling."""
        if not isinstance(status, Status):
            raise ValueError("A valid chair status is required")
        return cls(
            positions=status.positions,
            massage=status.massage,
            heat=status.heat,
            heat_levels=tuple(status.heat_level(zone) for zone in HEAT_ZONES),
            massage_timer_seconds=int.from_bytes(status.raw[6:8], "big"),
            heat_timer_seconds=int.from_bytes(status.raw[8:10], "big"),
            massage_continuous=massage_continuous,
            heat_continuous=heat_continuous,
        )

    @property
    def recall_massage_seconds(self) -> int:
        """An active Continuous recall starts the same native timer as manual On."""
        if self.massage_continuous and self.massage != "Off":
            return CONTINUOUS_MASSAGE_MINUTES * 60
        return self.massage_timer_seconds

    @property
    def recall_heat_seconds(self) -> int:
        if self.heat_continuous and self.heat:
            return CONTINUOUS_HEAT_MINUTES * 60
        return self.heat_timer_seconds

    def command(self, current: Status, slot: int) -> bytes:
        """Apply saved settings using current unknown context and captured format.

        The full numeric-memory format is captured. HA owns profile retention;
        this does not establish persistent chair storage or remote synchronization.
        """
        validate_slot(slot)
        if not isinstance(current, Status):
            raise ValueError("A valid current chair status is required")
        frame = bytearray(current.command(motors=self.positions))
        frame[6:8] = self.recall_massage_seconds.to_bytes(2, "big")
        frame[8:10] = self.recall_heat_seconds.to_bytes(2, "big")
        frame[10] = 0x10 + slot
        frame[17] = MASSAGE_MODES.index(self.massage)
        frame[18] = 0
        frame[19] = 1 if self.heat else 2
        frame[20:23] = bytes(HEAT_LEVELS.index(level) for level in self.heat_levels)
        frame[25] = checksum(frame)
        return bytes(frame)

    def matches_settings(self, status: Status) -> bool:
        """Check option acceptance separately from positions still in motion."""
        return (
            status.massage == self.massage
            and status.heat is self.heat
            and tuple(status.heat_level(zone) for zone in HEAT_ZONES) == self.heat_levels
            and int.from_bytes(status.raw[6:8], "big") == self.recall_massage_seconds
            and int.from_bytes(status.raw[8:10], "big") == self.recall_heat_seconds
        )

    def to_dict(self) -> dict:
        """Return a detached JSON-ready representation, never a raw packet."""
        return {
            "positions": list(self.positions),
            "massage": self.massage,
            "heat": self.heat,
            "heat_levels": list(self.heat_levels),
            "massage_timer_seconds": self.massage_timer_seconds,
            "heat_timer_seconds": self.heat_timer_seconds,
            "massage_continuous": self.massage_continuous,
            "heat_continuous": self.heat_continuous,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryProfile":
        """Validate persisted data before permitting any later recall."""
        expected = {
            "positions",
            "massage",
            "heat",
            "heat_levels",
            "massage_timer_seconds",
            "heat_timer_seconds",
        }
        if type(data) is not dict or set(data) not in (
            expected,
            expected | {"massage_continuous", "heat_continuous"},
        ):
            raise ValueError("Invalid saved memory profile")
        if type(data["positions"]) is not list or type(data["heat_levels"]) is not list:
            raise ValueError("Invalid saved memory positions or heat levels")
        return cls(
            positions=tuple(data["positions"]),
            massage=data["massage"],
            heat=data["heat"],
            heat_levels=tuple(data["heat_levels"]),
            massage_timer_seconds=data["massage_timer_seconds"],
            heat_timer_seconds=data["heat_timer_seconds"],
            massage_continuous=data.get("massage_continuous", False),
            heat_continuous=data.get("heat_continuous", False),
        )
