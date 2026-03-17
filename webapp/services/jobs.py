from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


@dataclass
class JobStatus:
    job_id: str
    state: str = "queued"  # queued|running|done|error
    current: int = 0
    total: int = 0
    message: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)


_lock = threading.Lock()
_jobs: Dict[str, JobStatus] = {}


def create_job() -> JobStatus:
    job_id = uuid.uuid4().hex
    st = JobStatus(job_id=job_id)
    with _lock:
        _jobs[job_id] = st
    return st


def get_job(job_id: str) -> JobStatus | None:
    with _lock:
        return _jobs.get(job_id)


def update_job(job_id: str, **kwargs) -> None:
    with _lock:
        st = _jobs.get(job_id)
        if not st:
            return
        for k, v in kwargs.items():
            if hasattr(st, k):
                setattr(st, k, v)


def run_job(job_id: str, fn: Callable[[], dict]) -> None:
    def target():
        update_job(job_id, state="running", message="Starting…")
        try:
            result = fn()
            update_job(job_id, state="done", result=result, message="Done")
        except Exception as e:  # noqa: BLE001
            update_job(job_id, state="error", error=str(e), message="Error")

    t = threading.Thread(target=target, daemon=True)
    t.start()

