"""Memory reservations retain Home completion and intent across delayed storage reads."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import status_with
from test_home import CAPTURED_HOME, HomeChair

from custom_components.svago.connection import Connection
from custom_components.svago.controller import CommandSuperseded, ControlError
from custom_components.svago.memory_protocol import MemoryProfile


class HomeMemoryRig(HomeChair):
    """Keep a sent Home and a real connection's memory lookup independently pending."""

    def __init__(self):
        super().__init__()
        self.home_written = asyncio.Event()
        self.home_release = asyncio.Event()
        self.load_entered = asyncio.Event()
        self.load_release = asyncio.Event()
        self.home_problem = None
        self.tasks = []
        self.controller._write = self.write_gated
        self.controller._sleep = self.sleep_fresh
        self.connection = Connection(SimpleNamespace(), "02:00:00:00:00:21")
        self.connection.controller = self.controller
        self.connection._client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
        self.connection._write_char = SimpleNamespace(max_write_without_response_size=256)
        self.connection._notifications_subscribed = self.connection._setup_complete = True
        profile = MemoryProfile.from_status(
            status_with(positions=(2, 3, 4, 5), heat=False, massage=0)
        )
        self.connection.memories = SimpleNamespace(
            async_load=AsyncMock(side_effect=self.load),
            get={2: profile}.get,
            async_save=AsyncMock(),
            async_close=AsyncMock(),
        )

    async def write_gated(self, packet):
        await self.write(packet)
        if packet == CAPTURED_HOME:
            self.home_written.set()
            await self.home_release.wait()
            if self.home_problem == "write_failure":
                raise OSError("simulated uncertain Home delivery")

    async def sleep_fresh(self, seconds):
        self.time += seconds
        if self.controller._home_session is not None:
            if self.home_problem == "timeout":
                self.time += 61
                self.controller.accept_status(self.controller.status)
            elif self.home_problem == "stale":
                self.time += 4
            else:
                self.report()
        else:
            self.controller.accept_status(self.controller.status)
        await asyncio.sleep(0)

    async def load(self):
        self.load_entered.set()
        await self.load_release.wait()

    def start(self, action):
        task = asyncio.create_task(action)
        self.tasks.append(task)
        return task

    async def begin_home_and_recall(self):
        home = self.start(self.controller.return_home())
        await asyncio.wait_for(self.home_written.wait(), 1)
        recall = self.start(self.connection.recall_memory(2))
        await asyncio.wait_for(self.load_entered.wait(), 1)
        assert self.writes == [CAPTURED_HOME]
        return home, recall

    async def finish_home(self, home):
        self.home_release.set()
        await asyncio.wait_for(home, 1)
        assert self.controller.last_home_request["outcome"] == "reported_neutral"


@pytest.fixture
async def rig():
    chair = HomeMemoryRig()
    yield chair
    for task in chair.tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*chair.tasks, return_exceptions=True)
    if not chair.connection._closed:
        await chair.connection.close()


def assert_no_followup(chair):
    assert chair.writes.count(CAPTURED_HOME) == 1
    assert all(packet[13:17] == b"\x80" * 4 for packet in chair.writes[1:])
    assert chair.controller.last_activation is None
    chair.connection.memories.async_save.assert_not_awaited()


@pytest.mark.parametrize(
    "problem", ["write_failure", "timeout", "stale", "cancel", "stop", "reconnect", "unload"]
)
async def test_failed_home_cannot_release_recall_after_storage_later_returns(rig, problem):
    home, recall = await rig.begin_home_and_recall()
    if problem in {"write_failure", "timeout", "stale"}:
        rig.home_problem = problem
        rig.home_release.set()
    elif problem == "cancel":
        home.cancel()
    elif problem == "stop":
        await rig.controller.stop_movement()
    elif problem == "reconnect":
        rig.controller.set_connected(False)
        rig.controller.set_connected(True)
        rig.controller.accept_status(status_with())
        rig.home_release.set()
    else:
        await rig.connection.close()
    result = await asyncio.gather(home, return_exceptions=True)
    assert isinstance(result[0], (ControlError, CommandSuperseded, asyncio.CancelledError))
    assert_no_followup(rig)
    if problem == "stale":
        # Restored feedback must not erase the failed Home attached to this
        # reservation while the storage operation was still pending.
        rig.controller.accept_status(rig.controller.status)
        assert rig.controller.available
    before = list(rig.writes)
    rig.load_release.set()
    with pytest.raises((ControlError, CommandSuperseded)) as rejected:
        await asyncio.wait_for(recall, 1)
    if problem in {"write_failure", "timeout", "stale", "cancel"}:
        assert "Return to neutral did not complete" in str(rejected.value)
    assert rig.writes == before
    assert_no_followup(rig)


async def test_successful_home_releases_disk_delayed_latest_memory_once(rig):
    home, recall = await rig.begin_home_and_recall()
    await rig.finish_home(home)
    assert rig.writes == [CAPTURED_HOME]
    assert not recall.done()
    rig.load_release.set()
    await asyncio.wait_for(recall, 1)
    assert len(rig.writes) == 3
    assert rig.writes[1][5] == 0x11
    assert rig.writes[2][5] == 0 and rig.writes[2][10] == 0x12
    assert rig.controller.status.positions == (2, 3, 4, 5)
    assert rig.controller.last_activation["post_home_settle"]["required_seconds"] == 0.25
    rig.connection.memories.async_save.assert_not_awaited()


@pytest.mark.parametrize("after_home", [False, True])
async def test_newer_manual_request_invalidates_disk_delayed_memory(rig, after_home):
    home, recall = await rig.begin_home_and_recall()
    if after_home:
        await rig.finish_home(home)
    manual = rig.start(rig.controller.step_position(0, 1))
    await asyncio.sleep(0)
    if not after_home:
        assert rig.writes == [CAPTURED_HOME]
        await rig.finish_home(home)
    await asyncio.wait_for(manual, 1)
    before = list(rig.writes)
    rig.load_release.set()
    with pytest.raises(CommandSuperseded):
        await asyncio.wait_for(recall, 1)
    assert rig.writes == before
    assert any(packet[13] == 0x82 for packet in rig.writes[1:])
    assert not any(packet[10] == 0x12 for packet in rig.writes[1:])


async def test_newer_memory_reservation_replaces_manual_before_disk_returns(rig):
    home = rig.start(rig.controller.return_home())
    await asyncio.wait_for(rig.home_written.wait(), 1)
    manual = rig.start(rig.controller.step_position(0, 1))
    await asyncio.sleep(0)
    recall = rig.start(rig.connection.recall_memory(2))
    await asyncio.wait_for(rig.load_entered.wait(), 1)
    await rig.finish_home(home)
    with pytest.raises(CommandSuperseded):
        await asyncio.wait_for(manual, 1)
    assert rig.writes == [CAPTURED_HOME]
    assert not recall.done()
    rig.load_release.set()
    await asyncio.wait_for(recall, 1)
    assert len(rig.writes) == 3
    assert rig.writes[1][5] == 0x11 and rig.writes[2][10] == 0x12
    assert not any(0x82 in packet[13:17] for packet in rig.writes)


async def test_cancelling_latest_disk_delayed_memory_does_not_revive_older_manual(rig):
    home = rig.start(rig.controller.return_home())
    await asyncio.wait_for(rig.home_written.wait(), 1)
    manual = rig.start(rig.controller.step_position(0, 1))
    await asyncio.sleep(0)
    recall = rig.start(rig.connection.recall_memory(2))
    await asyncio.wait_for(rig.load_entered.wait(), 1)
    recall.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recall
    assert not home.done()
    await rig.finish_home(home)
    with pytest.raises(CommandSuperseded):
        await asyncio.wait_for(manual, 1)
    rig.load_release.set()
    await asyncio.sleep(0)
    assert rig.writes == [CAPTURED_HOME]
    assert rig.controller.last_activation is None
