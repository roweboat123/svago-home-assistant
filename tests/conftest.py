"""Captured status fixture and deterministic transport."""

import asyncio

import pytest

from custom_components.svago.controller import Controller
from custom_components.svago.protocol import Status, checksum

TV_STATUS = bytes.fromhex("7E01020010000A8C0E100300000506101C0000010202020000087E")


def status_with(**changes):
    raw = bytearray(TV_STATUS)
    for key, value in changes.items():
        if key == "heat":
            raw[19] = 1 if value else 2
        elif key == "massage":
            raw[17] = value
        elif key == "positions":
            raw[13:17] = bytes(value)
        else:
            raw[int(key)] = value
    raw[25] = checksum(raw)
    return Status(bytes(raw))


class FakeChair:
    def __init__(self):
        self.writes = []
        self.time = 0.0
        self.on_sleep = None
        self.on_write = None
        self.feedback = True
        # Tests can acknowledge startup while deliberately withholding later
        # movement feedback. None retains the existing all-packet behavior.
        self.activation_feedback: bool | None = None
        self.fail_at = None
        self.controller = Controller(
            self.write, clock=lambda: self.time, sleep=self.sleep, feedback_timeout=0.05
        )
        self.controller.set_connected(True)
        self.controller.accept_status(status_with())

    async def write(self, payload):
        self.writes.append(payload)
        if self.fail_at == len(self.writes):
            raise OSError("simulated transport failure")
        if self.on_write:
            self.on_write(payload)
        feedback = self.feedback
        if payload[5] == 0x11 and self.activation_feedback is not None:
            feedback = self.activation_feedback
        if feedback:
            raw = bytearray(payload)
            raw[1:3] = b"\x01\x02"
            raw[18] = 0  # The chair does not report lumbar pressure/action here.
            if raw[19] == 0x8F:  # Settings writes preserve the master heat state.
                raw[19] = self.controller.status.raw[19]
            for i in range(13, 17):
                if raw[i] in (0x81, 0x82):
                    raw[i] = self.controller.status.raw[i] + (1 if raw[i] == 0x82 else -1)
                elif raw[i] >= 0x80:
                    raw[i] = self.controller.status.raw[i]
            raw[25] = checksum(raw)
            self.controller.accept_status(Status(bytes(raw)))

    async def sleep(self, seconds):
        self.time += seconds
        if self.on_sleep:
            self.on_sleep()
        await asyncio.sleep(0)


@pytest.fixture
def chair():
    return FakeChair()
