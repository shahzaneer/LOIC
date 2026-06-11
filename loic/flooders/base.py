from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from loic.req_state import ReqState


logger = logging.getLogger(__name__)


class AsyncFlooder(ABC):
    def __init__(self):
        self.state: ReqState = ReqState.IDLE
        self._is_flooding: bool = False
        self._task: asyncio.Task | None = None
        self._start_time: float = 0.0
        self._latencies: list[float] = []
        self.requested: int = 0
        self.downloaded: int = 0
        self.failed: int = 0
        self.bytes_sent: int = 0
        self.bytes_received: int = 0
        self.status_codes: dict[int, int] = {}

    @property
    def is_flooding(self) -> bool:
        return self._is_flooding

    @property
    def elapsed(self) -> float:
        if self._start_time == 0:
            return 0.0
        return time.monotonic() - self._start_time

    def record_latency(self, latency: float):
        if len(self._latencies) < 1000:
            self._latencies.append(latency)

    @property
    def avg_latency(self) -> float:
        if not self._latencies:
            return 0.0
        return sum(self._latencies) / len(self._latencies)

    def start(self, loop: asyncio.AbstractEventLoop | None = None):
        if self._is_flooding:
            return
        self._is_flooding = True
        self._start_time = time.monotonic()
        self._latencies.clear()
        try:
            self._task = asyncio.ensure_future(self._run())
        except RuntimeError:
            self._task = asyncio.ensure_future(self._run())

    async def start_async(self):
        if self._is_flooding:
            return
        self._is_flooding = True
        self._start_time = time.monotonic()
        self._latencies.clear()
        self._task = asyncio.ensure_future(self._run())

    def stop(self):
        self._is_flooding = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def stop_async(self):
        self._is_flooding = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @abstractmethod
    async def _run(self):
        ...

    async def cleanup(self):
        pass