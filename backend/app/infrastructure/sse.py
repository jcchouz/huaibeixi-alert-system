from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List

import redis
import redis.asyncio as redis_async
from fastapi import Request
from fastapi.responses import StreamingResponse

from app.core.serialization import json_dumps, serialize_val


class SSEHub:
    def __init__(self, redis_url: str | None = None, redis_channel: str | None = None) -> None:
        self._queues: List[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._redis_url = redis_url
        self._redis_channel = redis_channel or "huaibeixi:alerts:events"
        self._instance_id = uuid.uuid4().hex
        self._redis_sync = (
            redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2)
            if redis_url
            else None
        )
        self._redis_async = (
            redis_async.Redis.from_url(redis_url, decode_responses=True)
            if redis_url
            else None
        )
        self._subscriber_task: asyncio.Task | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def start(self) -> None:
        if self._redis_async is None or self._subscriber_task is not None:
            return
        self._subscriber_task = asyncio.create_task(
            self._subscribe_redis(),
            name="sse-redis-subscriber",
        )

    async def stop(self) -> None:
        if self._subscriber_task is not None:
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except asyncio.CancelledError:
                pass
            self._subscriber_task = None
        if self._redis_async is not None:
            await self._redis_async.aclose()
        if self._redis_sync is not None:
            self._redis_sync.close()

    @property
    def connection_count(self) -> int:
        return len(self._queues)

    def publish_threadsafe(self, payload: Dict[str, Any]) -> None:
        if self._loop is None or self._loop.is_closed():
            return

        try:
            self._loop.call_soon_threadsafe(self._push_local, payload)
        except RuntimeError:
            pass
        if self._redis_sync is not None:
            try:
                self._redis_sync.publish(
                    self._redis_channel,
                    json.dumps(
                        {"source": self._instance_id, "payload": payload},
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                pass

    def _push_local(self, payload: Dict[str, Any]) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                continue

    async def _subscribe_redis(self) -> None:
        if self._redis_async is None:
            return
        pubsub = self._redis_async.pubsub()
        await pubsub.subscribe(self._redis_channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    envelope = json.loads(message.get("data") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if envelope.get("source") == self._instance_id:
                    continue
                payload = envelope.get("payload", envelope)
                self._push_local(payload)
        finally:
            await pubsub.unsubscribe(self._redis_channel)
            await pubsub.aclose()

    async def event_stream(self, request: Request, engine: Any) -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues.append(queue)
        try:
            init_payload = json_dumps(
                {
                    "type": "CONNECTED",
                    "serverTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "engineStatus": engine.get_status(),
                }
            )
            yield f"event: connected\ndata: {init_payload}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    event_name = "alert" if event.get("type") == "ALERT_UPSERT" else "engine"
                    yield f"event: {event_name}\ndata: {json_dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    ping_data = json_dumps(
                        {
                            "type": "PING",
                            "serverTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "watermark": serialize_val(engine.current_watermark),
                        }
                    )
                    yield f"event: ping\ndata: {ping_data}\n\n"
        finally:
            if queue in self._queues:
                self._queues.remove(queue)

    def response(self, request: Request, engine: Any) -> StreamingResponse:
        return StreamingResponse(
            self.event_stream(request, engine),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
