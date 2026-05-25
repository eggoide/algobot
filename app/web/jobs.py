"""Async backtest job runner with status polling.

In-memory job store (single Flask worker, threaded). Each job runs in a daemon thread
and writes status into a dict. The HTTP layer reads from the same dict.

If you ever need multi-worker gunicorn, swap this for Redis/sqlite-backed queue.
"""

from __future__ import annotations

import threading
import traceback
import uuid
import time
import datetime
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, Optional


JobStatus = str  # "queued" | "running" | "done" | "error"


@dataclass
class Job:
    id: str
    kind: str  # "single", "replay", "optimize", "walk_forward"
    params: Dict[str, Any]
    status: JobStatus = "queued"
    progress: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z")
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Don't serialize the raw result here — caller fetches /api/job/{id}/result separately
        if d.get("result") is not None:
            d["result"] = "(available)"
        return d


class JobStore:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, params: Dict[str, Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, params=params)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict() for j in jobs[:limit]]

    def run_async(self, job: Job, fn: Callable[["Job"], Dict[str, Any]]) -> None:
        """Start fn(job) in a background thread. fn may mutate job.progress."""
        def runner():
            job.status = "running"
            job.started_at = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
            try:
                result = fn(job)
                job.result = result
                job.status = "done"
            except Exception as e:
                job.error = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
                job.status = "error"
            finally:
                job.finished_at = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

        threading.Thread(target=runner, daemon=True, name=f"job-{job.id}").start()


# Singleton — Flask app imports this
JOBS = JobStore()
