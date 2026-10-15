"""The analysis queue: one analysis at a time, results kept on disk.

Each job first computes the quick view (closed candles, no LLM: seconds) so the
asker gets numbers at once, then the multi-agent graph (minutes) through
``bot.analysis``, whose report is saved as Markdown and HTML under
``data/results/reports``. Listeners (the Telegram bot) are told when each
stage lands. When the graph or the AI keys are unavailable the job ends with
the quick view and a clear note instead of an error.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bot.analysis import AnalysisUnavailable, Analyst, fallback_message
from bot.settings import Settings
from cryptotrader.data.files import write_text_atomic

logger = logging.getLogger(__name__)

MAX_JOBS_KEPT = 200
ACTIVE = ("queued", "quick", "running")


class QueueFull(ValueError):
    """Too many requests waiting; the message is shown to the asker."""


@dataclass
class Job:
    id: str
    ticker: str                    # the pair, e.g. BTCUSDT
    date: str                      # the UTC day of the last closed candle analysed
    source: str                    # "web" | "telegram"
    chat_id: int | None = None
    status: str = "queued"         # queued | quick | running | done | failed
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    action: str = ""
    quick_summary: str = ""
    final_decision: str = ""
    guard_notes: str = ""
    note: str = ""                 # why the AI part did not run (shown instead of a report)
    report_dir: str = ""
    error: str = ""
    # Everyone who asked for this pair and day while it was waiting or running:
    # the same analysis serves them all instead of running twice.
    chat_ids: list[int] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.chat_id is not None and self.chat_id not in self.chat_ids:
            self.chat_ids.append(self.chat_id)
        if self.source and self.source not in self.sources:
            self.sources.append(self.source)

    @property
    def html_path(self) -> Path | None:
        path = Path(self.report_dir) / "complete_report.html" if self.report_dir else None
        return path if path and path.exists() else None


Listener = Callable[[Job, str], None]   # (job, event) with event "quick" | "done" | "failed"


class JobQueue:
    def __init__(self, settings: Settings, trader=None):
        self.settings = settings
        self.trader = trader               # for the quick view's "in the bot's basket" line
        self._queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []
        self.analyst = Analyst()
        self._path = settings.data_dir / "jobs.json"
        self._load()
        self._worker = threading.Thread(target=self._run, name="analysis-worker", daemon=True)

    # ── lifecycle ──
    def start(self) -> None:
        self._worker.start()

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    # ── public API ──
    def submit(self, ticker: str, date: str, source: str, chat_id: int | None = None) -> Job:
        """Queue an analysis, or join the one already waiting/running for this pair and day.

        Requests from people are refused with QueueFull once ``max_pending`` of them wait.
        """
        with self._lock:
            for existing in self._jobs.values():
                if existing.ticker == ticker and existing.date == date and existing.status in ACTIVE:
                    if chat_id is not None and chat_id not in existing.chat_ids:
                        existing.chat_ids.append(chat_id)
                    if source not in existing.sources:
                        existing.sources.append(source)
                    self._save()
                    return existing
            waiting = sum(1 for j in self._jobs.values() if j.status == "queued")
            if waiting >= self.settings.max_pending:
                raise QueueFull(
                    f"Hàng đợi đang có {waiting} lượt chờ (tối đa {self.settings.max_pending}). "
                    "Thử lại sau ít phút."
                )
            job = Job(id=uuid.uuid4().hex[:10], ticker=ticker, date=date, source=source, chat_id=chat_id)
            self._jobs[job.id] = job
            self._trim()
            self._save()
        self._queue.put(job.id)
        return job

    def is_active(self) -> bool:
        with self._lock:
            return any(j.status in ACTIVE for j in self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 30) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)[:limit]

    def position(self, job_id: str) -> int:
        """How many jobs run before this one (0 = running or next)."""
        with self._lock:
            waiting = [j for j in self._jobs.values() if j.status == "queued"]
        waiting.sort(key=lambda j: j.created)
        ids = [j.id for j in waiting]
        return ids.index(job_id) if job_id in ids else 0

    # ── worker ──
    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            self.process(job)

    def process(self, job: Job) -> None:
        try:
            self._process(job)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the worker
            logger.exception("Job %s (%s) failed", job.id, job.ticker)
            job.status, job.error, job.finished = "failed", str(exc)[:500], time.time()
            self._persist()
            self._notify(job, "failed")

    def _process(self, job: Job) -> None:
        from bot.quick import quick_view

        job.status, job.started = "quick", time.time()
        self._persist()
        try:
            job.quick_summary = quick_view(job.ticker, job.date, trader=self.trader)
        except Exception as exc:  # noqa: BLE001 - the AI part may still run
            logger.warning("Quick view %s %s failed: %s", job.ticker, job.date, exc)
            job.quick_summary = f"Không tính được số liệu nhanh: {str(exc)[:200]}"
        self._persist()
        self._notify(job, "quick")

        job.status = "running"
        self._persist()
        report_dir = self.settings.data_dir / "results" / "reports" / f"{job.ticker}_{job.date}_{job.id}"
        try:
            result = self.analyst.run(job.ticker, job.date, report_dir, portfolio=self._portfolio(job.date),
                                      config={"engine_strategy": self.settings.signal_strategy})
        except AnalysisUnavailable as exc:
            job.note = fallback_message(job.ticker, str(exc))
            job.status, job.finished = "done", time.time()
            self._persist()
            self._notify(job, "done")
            return
        job.action = result.rating
        job.final_decision = result.final_decision
        job.guard_notes = result.guard_notes
        job.report_dir = str(result.report_dir)
        job.status, job.finished = "done", time.time()
        self._persist()
        self._prune_reports()
        self._notify(job, "done")

    def _portfolio(self, day: str) -> dict | None:
        """The bot's book for the analysis (None when unknown or not of that day)."""
        if self.trader is None:
            return None
        try:
            return self.trader.analysis_portfolio(day)
        except Exception:  # noqa: BLE001 - the analysis runs without it
            logger.exception("No portfolio for the analysis of %s", day)
            return None

    def _notify(self, job: Job, event: str) -> None:
        for listener in self._listeners:
            try:
                listener(job, event)
            except Exception:  # noqa: BLE001 - a listener failure is not the job's
                logger.exception("Listener failed for job %s (%s)", job.id, event)

    # ── storage ──
    def _persist(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        data = [asdict(j) for j in self._jobs.values()]
        write_text_atomic(self._path, json.dumps(data, ensure_ascii=False))

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("jobs.json unreadable; starting with no history")
            return
        for row in rows if isinstance(rows, list) else []:
            try:
                job = Job(**{k: v for k, v in row.items() if k in Job.__dataclass_fields__})
            except (TypeError, AttributeError):
                continue
            if job.status in ACTIVE:
                # The process stopped mid-job: say so rather than show it running forever.
                job.status, job.error = "failed", "máy chủ khởi động lại khi đang phân tích"
            self._jobs[job.id] = job

    def _trim(self) -> None:
        if len(self._jobs) <= MAX_JOBS_KEPT:
            return
        for job in sorted(self._jobs.values(), key=lambda j: j.created)[: len(self._jobs) - MAX_JOBS_KEPT]:
            if job.status in ("done", "failed"):
                self._jobs.pop(job.id, None)

    def _prune_reports(self) -> None:
        """Keep the newest ``keep_reports`` report folders (the host's disk is small)."""
        root = self.settings.data_dir / "results" / "reports"
        if not root.exists():
            return
        folders = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in folders[self.settings.keep_reports:]:
            shutil.rmtree(old, ignore_errors=True)
