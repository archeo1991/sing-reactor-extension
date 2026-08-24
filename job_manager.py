import queue
import threading
import time
import uuid
from dataclasses import dataclass, field


class JobQueueFull(Exception):
    pass


class JobCancelled(Exception):
    pass


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


@dataclass
class Job:
    id: str
    request: object
    status: str = "queued"
    stage: str = "queued"
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def snapshot(self):
        payload = {"jobId": self.id, "status": self.status, "stage": self.stage}
        if self.status == "cancelling":
            payload["cancelRequested"] = True
        if self.result is not None:
            payload["result"] = self.result
        if self.error:
            payload["error"] = self.error
        return payload


class JobManager:
    def __init__(self, runner, workers=2, queue_size=8, retention_seconds=3600):
        self.runner = runner
        self.workers = workers
        self.queue_size = queue_size
        self.retention_seconds = retention_seconds
        self._queue = queue.Queue()
        self._jobs = {}
        self._lock = threading.Lock()
        self._threads = []
        for index in range(workers):
            thread = threading.Thread(target=self._worker, name=f"transcribe-worker-{index + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)
        self._cleaner = threading.Thread(target=self._cleanup_loop, name="transcribe-job-cleaner", daemon=True)
        self._cleaner.start()

    def create(self, request):
        job = Job(id=uuid.uuid4().hex, request=request)
        with self._lock:
            self._prune_locked()
            queued = sum(existing.status == "queued" for existing in self._jobs.values())
            if queued >= self.queue_size:
                raise JobQueueFull("识别任务队列已满，请稍后重试")
            self._jobs[job.id] = job
            self._queue.put_nowait(job.id)
        return job.snapshot()

    def get(self, job_id):
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    def cancel(self, job_id):
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job.status in TERMINAL_STATUSES:
                return job.snapshot()
            job.cancel_event.set()
            if job.status == "queued":
                job.status = "cancelled"
                job.stage = "cancelled"
            else:
                job.status = "cancelling"
                job.stage = "cancelling"
            job.updated_at = time.time()
            return job.snapshot()

    def capabilities(self):
        with self._lock:
            self._prune_locked()
            counts = {status: 0 for status in ("queued", "running", "cancelling", "succeeded", "failed", "cancelled")}
            for job in self._jobs.values():
                counts[job.status] = counts.get(job.status, 0) + 1
        return {"enabled": True, "workers": self.workers, "queue_size": self.queue_size, "counts": counts}

    def _set_stage(self, job, stage):
        with self._lock:
            if job.status == "running":
                job.stage = stage
                job.updated_at = time.time()

    def _worker(self):
        while True:
            job_id = self._queue.get()
            job = None
            try:
                with self._lock:
                    self._prune_locked()
                    job = self._jobs.get(job_id)
                    if not job or job.status != "queued" or job.cancel_event.is_set():
                        continue
                    job.status = "running"
                    job.stage = "starting"
                    job.updated_at = time.time()
                result = self.runner(job.request, job.cancel_event, lambda stage: self._set_stage(job, stage))
                with self._lock:
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                        job.stage = "cancelled"
                    else:
                        job.status = "succeeded"
                        job.stage = "completed"
                        job.result = result
                    job.updated_at = time.time()
                    self._prune_locked()
            except JobCancelled:
                if job is not None:
                    with self._lock:
                        job.status = "cancelled"
                        job.stage = "cancelled"
                        job.updated_at = time.time()
                        self._prune_locked()
            except Exception as exc:
                if job is not None:
                    with self._lock:
                        if job.cancel_event.is_set():
                            job.status = "cancelled"
                            job.stage = "cancelled"
                        else:
                            job.status = "failed"
                            job.stage = "failed"
                            job.error = str(exc)
                        job.updated_at = time.time()
                        self._prune_locked()
            finally:
                self._queue.task_done()

    def _cleanup_loop(self):
        interval = max(0.1, min(60.0, self.retention_seconds / 2))
        while True:
            time.sleep(interval)
            with self._lock:
                self._prune_locked()

    def _prune_locked(self):
        cutoff = time.time() - self.retention_seconds
        stale = [job_id for job_id, job in self._jobs.items()
                 if job.status in TERMINAL_STATUSES and job.updated_at < cutoff]
        for job_id in stale:
            self._jobs.pop(job_id, None)
