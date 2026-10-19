from __future__ import annotations

import asyncio

from app.infrastructure.sse import SSEHub


def test_local_sse_fanout_is_thread_safe() -> None:
    async def scenario() -> dict:
        hub = SSEHub()
        hub.bind_loop(asyncio.get_running_loop())
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        hub._queues.append(queue)
        hub.publish_threadsafe({"type": "ALERT_UPSERT", "alert": {"id": "1"}})
        await asyncio.sleep(0)
        return queue.get_nowait()

    event = asyncio.run(scenario())
    assert event["type"] == "ALERT_UPSERT"
    assert event["alert"]["id"] == "1"


def test_queue_full_does_not_break_fanout() -> None:
    async def scenario() -> None:
        hub = SSEHub()
        hub.bind_loop(asyncio.get_running_loop())
        first: asyncio.Queue = asyncio.Queue(maxsize=1)
        second: asyncio.Queue = asyncio.Queue(maxsize=1)
        first.put_nowait({"id": "existing"})
        hub._queues.extend([first, second])
        hub.publish_threadsafe({"id": "next"})
        await asyncio.sleep(0)
        assert second.get_nowait()["id"] == "next"

    asyncio.run(scenario())
