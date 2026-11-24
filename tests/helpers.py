"""Shared test helpers."""
import os
import shutil
import tempfile
import threading
import time
import unittest

from catalogiq.db import Database
from catalogiq.llm import MockProvider
from catalogiq.pipeline import Pipeline


class ScriptedRandom:
    """Stands in for random.Random: returns the given values, then 1.0 (no failure)."""

    def __init__(self, values):
        self._values = list(values)
        self._lock = threading.Lock()

    def random(self):
        with self._lock:
            return self._values.pop(0) if self._values else 1.0


class CountingMock(MockProvider):
    """The mock provider, plus its own count of calls in progress, so the tests
    do not rely only on the pipeline's metrics to check the concurrency limit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    def enrich(self, raw_title, raw_description):
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            return super().enrich(raw_title, raw_description)
        finally:
            with self._lock:
                self.in_flight -= 1


def products(count, prefix="SKU", title="Product number %d"):
    return [{"sku": "%s-%04d" % (prefix, i), "raw_title": title % i, "raw_description": ""}
            for i in range(count)]


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="catalogiq-test-")
        self.db_path = os.path.join(self.tmp, "test.db")
        self.db = Database(self.db_path)
        self._pipelines = []

    def tearDown(self):
        for pipeline in self._pipelines:
            pipeline.stop()
            pipeline.db.close_current()
        self.db.close_current()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_pipeline(self, provider, concurrency=5, start=True, db=None, **kwargs):
        pipeline = Pipeline(db or self.db, provider, concurrency, **kwargs)
        self._pipelines.append(pipeline)
        if start:
            pipeline.start()
        return pipeline

    def wait_for_job(self, job_id, timeout=15, db=None):
        db = db or self.db
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = db.get_job(job_id)
            if job["status"] == "completed":
                return job
            time.sleep(0.01)
        self.fail("job %s did not complete in %ss: %r" % (job_id, timeout, db.get_job(job_id)))
