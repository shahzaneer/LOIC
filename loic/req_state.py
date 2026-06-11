from __future__ import annotations

from enum import Enum


class ReqState(Enum):
    IDLE = "Idle"
    CONNECTING = "Connecting"
    REQUESTING = "Requesting"
    DOWNLOADING = "Downloading"
    COMPLETED = "Completed"
    FAILED = "Failed"