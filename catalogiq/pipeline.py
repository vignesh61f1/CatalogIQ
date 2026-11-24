"""The enrichment pipeline: background workers around the LLM.

How one item is processed:

1. Work out its content key (normalised title + description).
2. Under one lock, decide which of three things it is:
   - already in the cache            -> reuse the result (cache hit, no LLM call)
   - same content is being enriched  -> wait for that call's result (cache hit)
   - new content                     -> this item becomes the "owner" and calls the LLM
3. The owner calls the LLM with retries. Every attempt holds a slot of one
   global semaphore, so at most LLM_CONCURRENCY calls run at once across all jobs.
"""
import hashlib
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from datetime import datetime, timezone

from .config import MAX_ATTEMPTS
from .llm import LLMError, parse_enrichment
from .metrics import Metrics

MAX_RETRY_WAIT_S = 30.0


class EnrichmentFailed(Exception):
    """All attempts for one piece of content failed."""


def content_key(raw_title, raw_description):
    """Two products with the same key have the same content."""
    text = "%s %s" % (raw_title, raw_description or "")
    normalised = " ".join(text.lower().split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


class Pipeline:
    def __init__(self, db, provider, concurrency=5, retry_base_ms=200, metrics=None,
                 sleep=time.sleep):
        self.db = db
        self.provider = provider
        self.concurrency = concurrency
        self.retry_base_ms = retry_base_ms
        self.metrics = metrics or Metrics()
        self._sleep = sleep

        # The concurrency limit: one slot per LLM call in progress.
        self._llm_slots = threading.BoundedSemaphore(concurrency)

        # De-duplication of content that is being enriched right now.
        self._dedupe_lock = threading.Lock()
        self._in_flight = {}  # content_key -> Future resolved by the owner

        self._queue = queue.Queue()
        self._stopping = threading.Event()
        self._started_jobs = set()
        self._threads = []

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        """Start the workers and re-queue anything left unfinished by a restart."""
        # More workers than LLM slots: a worker that is sleeping between retries
        # or waiting for a duplicate should not keep an LLM slot idle.
        for index in range(self.concurrency * 2):
            thread = threading.Thread(
                target=self._worker, name="worker-%d" % index, daemon=True)
            thread.start()
            self._threads.append(thread)
        for item_id in self.db.pending_item_ids():
            self._queue.put(item_id)

    def stop(self):
        """Stop the workers. Items still queued stay 'pending' in the database
        and are picked up again by the next start()."""
        self._stopping.set()
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []

    # ---- submitting ------------------------------------------------------

    def submit_job(self, products):
        """Persist the job and queue its items. Returns immediately."""
        job_id = "j_" + uuid.uuid4().hex[:8]
        items = [
            (p["sku"], p["raw_title"], p["raw_description"],
             content_key(p["raw_title"], p["raw_description"]))
            for p in products
        ]
        item_ids = self.db.create_job(job_id, items, utc_now())
        for item_id in item_ids:
            self._queue.put(item_id)
        return self.db.get_job(job_id)

    # ---- workers ---------------------------------------------------------

    def _worker(self):
        while True:
            item_id = self._queue.get()
            if item_id is None or self._stopping.is_set():
                self.db.close_current()
                return
            item = None
            try:
                item = self.db.get_item(item_id)
                if item and item["state"] == "pending":
                    self._process(item)
            except Exception as exc:  # never let one bad item kill a worker
                if item:
                    try:
                        self.db.finish_item(item, utc_now(), error="internal error: %s" % exc)
                    except Exception:
                        pass

    def _process(self, item):
        if item["job_id"] not in self._started_jobs:
            self.db.mark_job_running(item["job_id"], utc_now())
            self._started_jobs.add(item["job_id"])

        key = item["content_key"]
        # The cache check and the in-flight registration must be one atomic step,
        # otherwise two workers could both decide they are the first to see `key`.
        with self._dedupe_lock:
            cached = self.db.cache_get(key)
            pending = None
            is_owner = False
            if cached is None:
                pending = self._in_flight.get(key)
                if pending is None:
                    pending = self._in_flight[key] = Future()
                    is_owner = True

        if cached is not None:
            self.db.finish_item(item, utc_now(), result=cached, cache_hit=True)
            return

        if not is_owner:
            # Someone else is already enriching this content: wait for them.
            try:
                result = pending.result()
            except EnrichmentFailed as exc:
                self.db.finish_item(item, utc_now(), error=str(exc))
            else:
                self.db.finish_item(item, utc_now(), result=result, cache_hit=True)
            return

        try:
            result = self._enrich_with_retries(item["raw_title"], item["raw_description"])
        except Exception as exc:
            failure = exc if isinstance(exc, EnrichmentFailed) else EnrichmentFailed(str(exc))
            try:
                self.db.finish_item(item, utc_now(), error=str(failure))
            finally:
                with self._dedupe_lock:
                    del self._in_flight[key]
                pending.set_exception(failure)
            return

        try:
            # Write the cache row before leaving the in-flight map, so a newcomer
            # always finds the result in one place or the other.
            self.db.finish_item(item, utc_now(), result=result, store_in_cache=True)
        finally:
            with self._dedupe_lock:
                del self._in_flight[key]
            pending.set_result(result)

    # ---- LLM calls -------------------------------------------------------

    def _enrich_with_retries(self, raw_title, raw_description):
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return parse_enrichment(self._call_llm(raw_title, raw_description))
            except LLMError as exc:  # provider failure or invalid output
                self.metrics.record_error()
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    # 200 ms, 400 ms, 800 ms. Sleep outside the semaphore so a
                    # backing-off item does not hold an LLM slot.
                    delay = self.retry_base_ms / 1000.0 * (2 ** (attempt - 1))
                    if exc.retry_after:
                        delay = max(delay, min(exc.retry_after, MAX_RETRY_WAIT_S))
                    self._sleep(delay)
        raise EnrichmentFailed(
            "LLM failed after %d attempts: %s" % (MAX_ATTEMPTS, last_error))

    def _call_llm(self, raw_title, raw_description):
        with self._llm_slots:
            self.metrics.call_started()
            try:
                return self.provider.enrich(raw_title, raw_description)
            except LLMError:
                raise
            except Exception as exc:
                raise LLMError("provider error: %s" % exc)
            finally:
                self.metrics.call_finished()
