from __future__ import annotations

import random
import string
import threading
import os
import struct
import time


_ntv = ["6.0", "6.1", "6.2", "6.3", "10.0"]
_firefox_versions = list(range(36, 130))
_lock = threading.Lock()


def random_string(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=length))


def random_bytes(length: int) -> bytes:
    return os.urandom(length)


def random_int(min_val: int, max_val: int) -> int:
    return random.randint(min_val, max_val)


def random_user_agent() -> str:
    nt = random.choice(_ntv)
    ff = random.choice(_firefox_versions)
    if random.random() >= 0.5:
        return f"Mozilla/5.0 (Windows NT {nt}; WOW64; rv:{ff}.0) Gecko/20100101 Firefox/{ff}.0"
    return f"Mozilla/5.0 (Windows NT {nt}; rv:{ff}.0) Gecko/20100101 Firefox/{ff}.0"


def random_element(array: list):
    if not array:
        return None
    if len(array) == 1:
        return array[0]
    return random.choice(array)


def random_http_header(
    method: str,
    subsite: str,
    host: str,
    subsite_random: bool = False,
    gzip: bool = False,
    keep_alive: int = 0,
    body: bytes | None = None,
    headers: dict | None = None,
) -> bytes:
    sub = subsite + (random_string() if subsite_random else "")
    ua = random_user_agent()
    accept_enc = "Accept-Encoding: gzip, deflate\r\n" if gzip else ""
    ka = f"Keep-Alive: {keep_alive}\r\nConnection: keep-alive\r\n" if keep_alive > 0 else ""

    extra_headers = ""
    if headers:
        for k, v in headers.items():
            extra_headers += f"{k}: {v}\r\n"

    content_length = ""
    if body is not None:
        content_length = f"Content-Length: {len(body)}\r\n"

    header = (
        f"{method} {sub} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: {ua}\r\n"
        f"Accept: */*\r\n"
        f"{extra_headers}"
        f"{accept_enc}"
        f"{content_length}"
        f"{ka}"
        f"{extra_headers}"
        f"\r\n"
    )
    if body is not None:
        return header.encode("ascii") + body
    return header.encode("ascii")


def build_tcp_payload(data: str, random_suffix: bool = False, min_length: int = 0) -> bytes:
    payload = data.encode("ascii")
    if random_suffix:
        padding = max(0, min_length - len(payload))
        payload += os.urandom(padding) if padding else random_string(random_int(4, 12)).encode("ascii")
    elif min_length > len(payload):
        payload += os.urandom(min_length - len(payload))
    return payload


def parse_int(s: str, min_val: int, max_val: int):
    try:
        val = int(s)
        if min_val <= val <= max_val:
            return True, val
        return False, 0
    except (ValueError, TypeError):
        return False, 0


def resolve_host(host: str, port: int = 0, family: int = 0) -> list[tuple[str, int]]:
    import socket as _socket
    try:
        results = _socket.getaddrinfo(host, port, family or _socket.AF_UNSPEC, _socket.SOCK_STREAM)
        return [(r[4][0], r[4][1]) for r in results]
    except _socket.gaierror:
        return []


def icmp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        word = (data[i] << 8) + data[i + 1]
        s += word
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF