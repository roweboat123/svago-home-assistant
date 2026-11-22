"""Svago's observed 27-byte protocol and explicitly captured native presets."""

from dataclasses import dataclass

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
FRAME_SIZE = 27
STATUS_HEADER = bytes.fromhex("7e01020010")
# All 388 normal control writes in the phone log use 00 at offset 5.
# Status reports also contain 01/11; these are not interchangeable with control
# fields. The user verified the separate startup packet turns the wired remote
# On and does not toggle it Off when repeated. Its selector is 11, not 00.
NORMAL_CONTROL_FIELD = 0x00
ACTIVATION_FIELD = 0x11
MASSAGE_MODES = ("Off", "Gentle", "Relax", "Stretch", "Wave", "Vigorous")
HEAT_ZONES = ("backrest", "seat", "footrest")
HEAT_LEVELS = ("Off", "Low", "Medium", "High")
TIMER_OPTIONS = {"heat": (60, 120, 180), "massage": (15, 30, 45)}
CONTINUOUS_MASSAGE_MINUTES = 45
CONTINUOUS_HEAT_MINUTES = 180
# These are complete captured invocations, not position-only commands or
# universal defaults. Both select Relax for 15 minutes and master heat On for
# 180 minutes, with backrest Low, seat Off and footrest Low. Native selector 10
# chooses the preset; motor bytes remain 8F rather than guessed final targets.
# Variants that preserve the caller's heat/massage settings are not yet verified.
PRESET_COMMANDS = {
    "zgr": bytes.fromhex("7E020100100003842A300100008F8F8F8F0200010100010000367E"),
    "lay_flat": bytes.fromhex("7E020100100003842A300200008F8F8F8F0200010100010000377E"),
}
POSITION_NAMES = ("headrest", "backrest", "seat", "footrest")
# Limits of the observed data, NOT calibrated mechanical endpoints.
# ZGR reports seat 40; Lay flat reports backrest 22. These are valid position
# feedback, including when ordinary controls are used after a native preset.
OBSERVED_BOUNDS = ((0, 17), (0, 22), (0, 40), (0, 28))


def checksum(frame: bytes | bytearray) -> int:
    return sum(frame[1:25]) & 0xFF


def preset_command(name: str) -> bytes:
    """Return the exact captured native invocation, including its other settings."""
    if not isinstance(name, str) or name not in PRESET_COMMANDS:
        raise ValueError("Unknown chair preset")
    return PRESET_COMMANDS[name]


@dataclass(frozen=True)
class Status:
    raw: bytes

    def __post_init__(self) -> None:
        if (
            len(self.raw) != FRAME_SIZE
            or not self.raw.startswith(STATUS_HEADER)
            or self.raw[-1] != 0x7E
            or self.raw[25] != checksum(self.raw)
        ):
            raise ValueError("Invalid Svago status frame")

    @property
    def heat(self) -> bool | None:
        return {1: True, 2: False}.get(self.raw[19])

    @property
    def massage(self) -> str | None:
        value = self.raw[17]
        return MASSAGE_MODES[value] if value < len(MASSAGE_MODES) else None

    @property
    def positions(self) -> tuple[int, ...]:
        return tuple(self.raw[13:17])

    @property
    def usable(self) -> bool:
        return self.heat is not None and self.massage is not None

    def heat_level(self, zone: str) -> str | None:
        """Return a captured zone setting; leave unknown values unknown."""
        value = self.raw[20 + HEAT_ZONES.index(zone)]
        return HEAT_LEVELS[value] if value < len(HEAT_LEVELS) else None

    def timer_minutes(self, kind: str) -> int | None:
        """Expose only confirmed standard timer settings, not guessed sentinels."""
        if kind not in TIMER_OPTIONS:
            raise ValueError("Unknown timer")
        offset = 8 if kind == "heat" else 6
        seconds = int.from_bytes(self.raw[offset : offset + 2], "big")
        minutes, remainder = divmod(seconds, 60)
        return minutes if not remainder and minutes in TIMER_OPTIONS[kind] else None

    def setting_command(
        self,
        *,
        zone: str | None = None,
        level: str | None = None,
        timer: str | None = None,
        minutes: int | None = None,
    ) -> bytes:
        """Set one mapped option while retaining current positions and context.

        Captured setting writes retain their current field 5 (00, 01 or 11)
        and use 8F for master heat. These differ from ordinary action commands.
        Holding the live numeric motor positions is a state-preserving adaptation;
        software tests cannot establish its behavior at every physical position.
        """
        zone_request = zone is not None or level is not None
        timer_request = timer is not None or minutes is not None
        if zone_request == timer_request:
            raise ValueError("Choose exactly one heat level or timer setting")
        if zone_request:
            if zone not in HEAT_ZONES or level not in HEAT_LEVELS:
                raise ValueError("Unknown heat zone or level")
        elif (
            timer not in TIMER_OPTIONS
            or type(minutes) is not int
            or minutes not in TIMER_OPTIONS[timer]
        ):
            raise ValueError("Choose a supported whole-minute timer setting")
        if not self.usable:
            raise ValueError("Unknown heat or massage state; refusing to guess")
        if any(value >= 0x80 for value in self.positions):
            raise ValueError("Current motor positions are not numeric; cannot prepare settings.")
        frame = bytearray(self.raw)
        frame[1:3] = b"\x02\x01"
        frame[18] = 0
        frame[19] = 0x8F
        if zone_request:
            frame[20 + HEAT_ZONES.index(zone)] = HEAT_LEVELS.index(level)
        else:
            offset = 8 if timer == "heat" else 6
            frame[offset : offset + 2] = (minutes * 60).to_bytes(2, "big")
        frame[25] = checksum(frame)
        return bytes(frame)

    def activation_command(self) -> bytes:
        """Adapt the verified On packet to current positions and settings.

        The captured packet's zero positions/settings are not universal defaults.
        Holding the reported numeric positions avoids inserting a home target.
        This live-state adaptation still needs physical validation on the chair.
        """
        if any(value >= 0x80 for value in self.positions):
            raise ValueError("Current motor positions are not numeric; cannot prepare activation.")
        frame = bytearray(self.command(motors=self.positions))
        frame[5] = ACTIVATION_FIELD
        frame[25] = checksum(frame)
        return bytes(frame)

    def continuous_massage_command(self, mode: str | None = None) -> bytes:
        """Renew one running mode without replaying heat On or motor movement.

        Repeating a 15-minute massage command is physically verified to renew
        it. Using the known native maximum of 45 minutes is an adaptation that
        still needs physical validation. Master heat 8F is the captured preserve
        marker; independent heat expiry during renewal also needs validation.
        """
        mode = self.massage if mode is None else mode
        if mode not in MASSAGE_MODES[1:]:
            raise ValueError("Continuous massage requires an active massage mode.")
        frame = bytearray(self.command(motors=(0xFF,) * 4, massage=mode))
        frame[6:8] = (CONTINUOUS_MASSAGE_MINUTES * 60).to_bytes(2, "big")
        frame[19] = 0x8F
        frame[25] = checksum(frame)
        return bytes(frame)

    def continuous_heat_command(self) -> bytes:
        """Renew heat with its standard maximum duration and current settings.

        Repeating Heat On renews its timer. No massage-preserve byte has been
        verified while massage is active: this retains the actual mode and
        timer, so the controller must defer automatic writes during finite or
        unmanaged massage. It must never infer an unlimited native duration.
        """
        frame = bytearray(self.command(motors=(0xFF,) * 4, heat=True))
        frame[8:10] = (CONTINUOUS_HEAT_MINUTES * 60).to_bytes(2, "big")
        frame[25] = checksum(frame)
        return bytes(frame)

    def command(
        self,
        *,
        motors: tuple[int, ...] = (0x8F,) * 4,
        lumbar: int = 0,
        heat: bool | None = None,
        massage: str | None = None,
    ) -> bytes:
        """Copy current options, override only explicit controls, then checksum."""
        if not self.usable:
            raise ValueError("Unknown heat or massage state; refusing to guess")
        if len(motors) != 4 or any(not 0 <= value <= 255 for value in motors):
            raise ValueError("Four byte-sized motor values are required")
        if lumbar not in (0, 1, 2):
            raise ValueError("Unknown lumbar action")
        frame = bytearray(self.raw)
        frame[1:3] = b"\x02\x01"
        frame[5] = NORMAL_CONTROL_FIELD
        frame[13:17] = bytes(motors)
        frame[18] = lumbar
        if heat is not None:
            # Zone levels and timer are now mapped. Retain explicit zone Off
            # selections and any existing duration. Only an unset zero duration
            # receives the previously verified 60-minute value on Heat On.
            if heat and frame[8:10] == b"\x00\x00":
                frame[8:10] = b"\x0e\x10"
            frame[19] = 1 if heat else 2
        if massage is not None:
            frame[17] = MASSAGE_MODES.index(massage)
        frame[25] = checksum(frame)
        return bytes(frame)


class FrameAssembler:
    """Reassemble fragmented/concatenated notifications and recover after loss."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes | bytearray) -> list[Status]:
        self._buffer.extend(data)
        frames = []
        while len(self._buffer) >= FRAME_SIZE:
            offset = self._buffer.find(STATUS_HEADER)
            if offset < 0:
                # Retain enough bytes for a header split over two notifications.
                del self._buffer[:-4]
                break
            del self._buffer[:offset]
            if len(self._buffer) < FRAME_SIZE:
                break
            try:
                status = Status(bytes(self._buffer[:FRAME_SIZE]))
            except ValueError:
                del self._buffer[:1]
            else:
                frames.append(status)
                del self._buffer[:FRAME_SIZE]
        return frames
