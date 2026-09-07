"""Background job registry for install/removal chains (in-memory).

register_target(install=True) and unregister_target(uninstall=True) run
their chains as background jobs and return a job id at once; the agent
polls job_status(job_id=...). The chains report step by step through a
sink (Job.extend), so a running job already shows the steps it passed.

Jobs live in the router process memory: a router restart loses them (the
chains are re-runnable — install is idempotent, a removal retry picks the
base state up). A capped history of finished jobs stays answerable.
"""

import threading
import time
import uuid

KIND_INSTALL = "установка"
KIND_REMOVAL = "удаление"

STATUS_RUNNING = "выполняется"
STATUS_DONE = "готово"
STATUS_ERROR = "ошибка"

KEEP_DONE = 30


class Job:
    """One background chain: identity, status and progress lines.

    extend() is the sink the installer chains append their step reports
    to (worker thread); lines() snapshots the report for job_status. A
    dedicated lock keeps the line list consistent without relying on the
    GIL.
    """

    def __init__(self, alias, kind):
        self.id = uuid.uuid4().hex[:8]
        self.alias = alias
        self.kind = kind
        self.started = time.strftime("%H:%M:%S")
        self.status = STATUS_RUNNING
        self._started_mono = time.monotonic()
        self._finished_mono = None
        self._lines = []
        self._lock = threading.Lock()

    def extend(self, items):
        with self._lock:
            self._lines.extend(items)

    def append(self, line):
        with self._lock:
            self._lines.append(line)

    def lines(self):
        with self._lock:
            return list(self._lines)

    def elapsed(self):
        end = self._finished_mono if self._finished_mono is not None else time.monotonic()
        return end - self._started_mono

    def _finish(self, status):
        self._finished_mono = time.monotonic()
        self.status = status


class JobRegistry:
    """Thread-safe id -> Job map with a capped history of finished jobs."""

    def __init__(self, keep_done=KEEP_DONE):
        self._lock = threading.Lock()
        self._jobs = {}
        self._keep_done = keep_done

    def start(self, alias, kind):
        with self._lock:
            job = Job(alias, kind)
            self._jobs[job.id] = job
            self._evict_locked()
            return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(str(job_id or ""))

    def running(self, alias):
        """First RUNNING job for the alias (any kind): install and removal
        must not overlap on one base (the configurator and the infobase
        lock tolerate no competition)."""
        with self._lock:
            for job in self._jobs.values():
                if job.alias == alias and job.status == STATUS_RUNNING:
                    return job
        return None

    def finish(self, job, status):
        with self._lock:
            job._finish(status)

    def snapshot(self):
        with self._lock:
            return list(self._jobs.values())[::-1]

    def _evict_locked(self):
        done = [jid for jid, job in self._jobs.items() if job.status != STATUS_RUNNING]
        while len(done) > self._keep_done:
            del self._jobs[done.pop(0)]


def describe(job):
    """job_status(job_id=...) answer: running (with progress so far) or
    the final report for done/error."""
    head = f"{job.kind} цели «{job.alias}»"
    stamp = f"старт {job.started}, прошло {job.elapsed():.0f} с"
    body = job.lines()
    if job.status == STATUS_RUNNING:
        text = "".join(line + "\n" for line in body)
        return (f"Задание {job.id}: {head} — ВЫПОЛНЯЕТСЯ ({stamp}).\n"
                f"{text}"
                f"Задание продолжается — повторяйте job_status(job_id=\"{job.id}\") "
                "через 20–30 с; повторный запуск той же операции по этой цели не нужен.")
    label = "ГОТОВО" if job.status == STATUS_DONE else "ОШИБКА"
    return (f"Задание {job.id}: {head} — {label} ({stamp}). Отчёт:\n" + "\n".join(body))


def listing(jobs):
    """job_status() without id: newest-first list of recent jobs."""
    if not jobs:
        return ("Фоновых заданий нет. Они появляются при register_target(install=true) "
                "и unregister_target(uninstall=true); статус — job_status(job_id=\"ид\").")
    lines = [f"Задания роутера ({len(jobs)}, новые сверху):"]
    for job in jobs:
        lines.append(f"  {job.id} — {job.kind} цели «{job.alias}» — {job.status} "
                     f"({job.elapsed():.0f} с, старт {job.started})")
    return "\n".join(lines)
