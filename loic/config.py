from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional
from loic.protocol import Protocol


@dataclass(frozen=True)
class AttackConfig:
    target_ip: str = ""
    target_host: str = ""
    port: int = 80
    method: Protocol = Protocol.TCP
    threads: int = 10
    delay: int = 0
    timeout: int = 30
    subsite: str = "/"
    data: str = "U dun goofed"
    wait_reply: bool = True
    random_sub: bool = False
    random_msg: bool = False
    use_get: bool = False
    allow_gzip: bool = False
    socks_per_thread: int = 25
    ipv6: bool = False
    use_tls: bool = False
    verify_response: bool = True
    rate_limit: int = 0
    ramp_up: float = 0.0
    jitter: float = 0.0
    payload_size: int = 0
    extra_headers: dict = field(default_factory=dict)
    duration: float = 0.0

    def effective_host(self) -> str:
        return self.target_host or self.target_ip

    def copy(self, **kwargs):
        return replace(self, **kwargs)