from __future__ import annotations

from enum import IntEnum


class Protocol(IntEnum):
    NONE = 0
    TCP = 1
    UDP = 2
    HTTP = 3
    SLOWLOIC = 4
    RECOIL = 5
    ICMP = 6

    @property
    def label(self) -> str:
        return {
            Protocol.NONE: "None",
            Protocol.TCP: "TCP",
            Protocol.UDP: "UDP",
            Protocol.HTTP: "HTTP",
            Protocol.SLOWLOIC: "SlowLoris",
            Protocol.RECOIL: "ReCoil",
            Protocol.ICMP: "ICMP",
        }[self]