from __future__ import annotations

import asyncio
import json
import csv
import io
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from loic.req_state import ReqState

logger = logging.getLogger(__name__)


@dataclass
class MetricsSnapshot:
    timestamp: float = 0.0
    requested: int = 0
    downloaded: int = 0
    failed: int = 0
    idle: int = 0
    connecting: int = 0
    requesting: int = 0
    downloading: int = 0
    is_flooding: bool = False
    elapsed: float = 0.0
    avg_latency: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0
    status_codes: dict = field(default_factory=dict)
    req_per_sec: float = 0.0
    bandwidth_out: float = 0.0
    bandwidth_in: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status_codes"] = dict(d.get("status_codes", {}))
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class MetricsCollector:
    def __init__(self, export_path: Optional[Path] = None, export_format: str = "json"):
        self._history: list[MetricsSnapshot] = []
        self._start_time: float = 0.0
        self._last_requested: int = 0
        self._export_path = export_path
        self._export_format = export_format
        self._prev_requested: int = 0
        self._prev_bytes_sent: int = 0
        self._prev_bytes_received: int = 0

    def start(self):
        self._start_time = time.monotonic()
        self._history.clear()
        self._prev_requested = 0
        self._prev_bytes_sent = 0
        self._prev_bytes_received = 0

    def record(self, snapshot: MetricsSnapshot):
        snapshot.timestamp = time.time()
        snapshot.elapsed = time.monotonic() - self._start_time

        if snapshot.elapsed > 0:
            snapshot.req_per_sec = snapshot.requested / snapshot.elapsed
            snapshot.bandwidth_out = snapshot.bytes_sent / snapshot.elapsed
            snapshot.bandwidth_in = snapshot.bytes_received / snapshot.elapsed

        self._history.append(snapshot)
        if len(self._history) > 3600:
            self._history = self._history[-3600:]

    @property
    def history(self) -> list[MetricsSnapshot]:
        return self._history

    @property
    def latest(self) -> Optional[MetricsSnapshot]:
        return self._history[-1] if self._history else None

    def summary(self) -> dict:
        if not self._history:
            return {}
        first = self._history[0]
        last = self._history[-1]
        total_req = last.requested
        total_dl = last.downloaded
        total_fail = last.failed
        elapsed = last.elapsed
        return {
            "total_requested": total_req,
            "total_downloaded": total_dl,
            "total_failed": total_fail,
            "elapsed_seconds": round(elapsed, 2),
            "avg_req_per_sec": round(total_req / elapsed, 2) if elapsed > 0 else 0,
            "total_bytes_sent": last.bytes_sent,
            "total_bytes_received": last.bytes_received,
            "avg_latency_ms": round(last.avg_latency * 1000, 2),
            "peak_req_per_sec": round(max((s.req_per_sec for s in self._history), default=0), 2),
            "status_codes": dict(last.status_codes),
            "failure_rate": round(total_fail / total_req * 100, 2) if total_req > 0 else 0,
        }

    def export_json(self, path: Path | None = None) -> str:
        target = path or self._export_path
        data = {
            "summary": self.summary(),
            "history": [s.to_dict() for s in self._history],
        }
        json_str = json.dumps(data, indent=2, default=str)
        if target:
            target.write_text(json_str)
        return json_str

    def export_csv(self, path: Path | None = None) -> str:
        target = path or self._export_path
        if not self._history:
            return ""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(asdict(self._history[0]).keys()))
        writer.writeheader()
        for snap in self._history:
            row = snap.to_dict()
            row["status_codes"] = json.dumps(row.get("status_codes", {}))
            writer.writerow(row)
        csv_str = buf.getvalue()
        if target:
            target.write_text(csv_str)
        return csv_str