"""Pipeline tests: concurrency limit, retries, de-duplication, crash recovery.

All of them run against the mock provider.
"""
import json
import time
import unittest

from catalogiq.db import Database
from catalogiq.llm import LLMError, MockProvider
from catalogiq.pipeline import content_key

from .helpers import CountingMock, PipelineTestCase, ScriptedRandom, products


class ConcurrencyLimitTests(PipelineTestCase):
    def test_never_more_than_limit_calls_at_once(self):
        provider = CountingMock(latency_ms=30, failure_rate=0)
        pipeline = self.make_pipeline(provider, concurrency=3)

        job = self.wait_for_job(pipeline.submit_job(products(40))["id"])

        self.assertEqual(job["done"], 40)
        self.assertEqual(provider.calls, 40)
        # Reaches the limit (work really is parallel) but never passes it.
        self.assertEqual(provider.max_in_flight, 3)
        self.assertEqual(pipeline.metrics.snapshot()["max_concurrent_llm_calls"], 3)

    def test_limit_is_shared_across_jobs(self):
        provider = CountingMock(latency_ms=30, failure_rate=0)
        pipeline = self.make_pipeline(provider, concurrency=4)

        jobs = [pipeline.submit_job(products(20, prefix="JOB%d" % n, title="job %d item %%d" % n))
                for n in range(3)]
        for job in jobs:
            self.wait_for_job(job["id"])

        self.assertEqual(provider.calls, 60)
        self.assertEqual(provider.max_in_flight, 4)

    def test_limit_holds_while_calls_are_failing_and_retrying(self):
        provider = CountingMock(latency_ms=10, failure_rate=0.5)
        pipeline = self.make_pipeline(provider, concurrency=2, retry_base_ms=5)

        self.wait_for_job(pipeline.submit_job(products(30))["id"])

        self.assertLessEqual(provider.max_in_flight, 2)

    def test_items_run_in_parallel(self):
        provider = MockProvider(latency_ms=100, failure_rate=0)
        pipeline = self.make_pipeline(provider, concurrency=5)

        started = time.time()
        self.wait_for_job(pipeline.submit_job(products(20))["id"])
        elapsed = time.time() - started

        # One at a time would take 20 * 0.1 = 2 s; five at a time takes about 0.4 s.
        self.assertLess(elapsed, 1.2)

    def test_submit_returns_before_the_work_is_done(self):
        pipeline = self.make_pipeline(MockProvider(latency_ms=200, failure_rate=0), concurrency=2)

        started = time.time()
        job = pipeline.submit_job(products(50))
        elapsed = time.time() - started

        self.assertLess(elapsed, 0.5)
        self.assertEqual(job["total"], 50)
        self.assertLess(job["done"], 50)
        self.assertIn(job["status"], ("queued", "running"))


class RetryTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.delays = []

    def make_retry_pipeline(self, provider):
        # Record the backoff delays instead of really sleeping.
        return self.make_pipeline(provider, concurrency=2, sleep=self.delays.append)

    def test_recovers_after_two_failures(self):
        # failure_rate 0.5: draws of 0.0 fail, the default draw of 1.0 succeeds.
        provider = CountingMock(latency_ms=0, failure_rate=0.5, rng=ScriptedRandom([0.0, 0.0]))
        pipeline = self.make_retry_pipeline(provider)

        job = self.wait_for_job(pipeline.submit_job(products(1))["id"])

        self.assertEqual((job["done"], job["failed"]), (1, 0))
        self.assertEqual(provider.calls, 3)
        self.assertEqual(self.delays, [0.2, 0.4])
        metrics = pipeline.metrics.snapshot()
        self.assertEqual(metrics["llm_calls_total"], 3)
        self.assertEqual(metrics["llm_errors_total"], 2)
        self.assertEqual(self.db.get_product("SKU-0000")["status"], "enriched")

    def test_gives_up_after_four_attempts(self):
        provider = CountingMock(latency_ms=0, failure_rate=1.0)
        pipeline = self.make_retry_pipeline(provider)

        job = self.wait_for_job(pipeline.submit_job(products(1))["id"])

        self.assertEqual((job["done"], job["failed"]), (0, 1))
        self.assertEqual(provider.calls, 4)  # 1 call + 3 retries
        self.assertEqual(self.delays, [0.2, 0.4, 0.8])  # exponential backoff
        product = self.db.get_product("SKU-0000")
        self.assertEqual(product["status"], "failed")
        self.assertIn("4 attempts", product["error"])
        self.assertIsNone(product["clean_title"])

    def test_one_failed_product_does_not_stop_the_job(self):
        class FailsOnBad(CountingMock):
            def enrich(self, raw_title, raw_description):
                if "bad" in raw_title:
                    raise LLMError("cannot enrich this one")
                return super().enrich(raw_title, raw_description)

        pipeline = self.make_retry_pipeline(FailsOnBad(latency_ms=0, failure_rate=0))
        items = products(10)
        items[4]["raw_title"] = "a bad listing"

        job = self.wait_for_job(pipeline.submit_job(items)["id"])

        self.assertEqual((job["total"], job["done"], job["failed"]), (10, 9, 1))
        self.assertEqual(self.db.get_product("SKU-0004")["status"], "failed")
        self.assertIn("cannot enrich this one", self.db.get_product("SKU-0004")["error"])
        self.assertEqual(self.db.get_product("SKU-0005")["status"], "enriched")

    def _run_with_first_answer(self, bad_answer):
        class BadFirstAnswer(CountingMock):
            def enrich(self, raw_title, raw_description):
                answer = super().enrich(raw_title, raw_description)
                return bad_answer if self.calls == 1 else answer

        provider = BadFirstAnswer(latency_ms=0, failure_rate=0)
        pipeline = self.make_retry_pipeline(provider)
        job = self.wait_for_job(pipeline.submit_job(products(1))["id"])
        return provider, pipeline, job

    def test_invalid_json_counts_as_a_failed_attempt(self):
        provider, pipeline, job = self._run_with_first_answer("Sure! Here is your JSON...")

        self.assertEqual(provider.calls, 2)
        self.assertEqual(pipeline.metrics.snapshot()["llm_errors_total"], 1)
        self.assertEqual(job["done"], 1)

    def test_unknown_category_counts_as_a_failed_attempt(self):
        bad = json.dumps({"clean_title": "X", "category": "Toys", "brand": None, "tags": []})
        provider, pipeline, job = self._run_with_first_answer(bad)

        self.assertEqual(provider.calls, 2)
        self.assertEqual(pipeline.metrics.snapshot()["llm_errors_total"], 1)
        self.assertEqual(job["done"], 1)


class DeduplicationTests(PipelineTestCase):
    def test_content_key_ignores_case_and_whitespace(self):
        self.assertEqual(content_key("  AMUL   butter 500G", "pck of\t2 "),
                         content_key("amul butter 500g", "PCK OF 2"))
        self.assertEqual(content_key("Amul Butter", None), content_key("amul butter", ""))
        self.assertNotEqual(content_key("amul butter 500g", ""), content_key("amul butter 100g", ""))

    def test_duplicates_processed_at_the_same_moment_make_one_call(self):
        provider = CountingMock(latency_ms=150, failure_rate=0)
        pipeline = self.make_pipeline(provider, concurrency=5)
        variants = ["Amul Butter 500g", "  AMUL   butter 500G ", "amul BUTTER 500g"]
        items = [{"sku": "D-%02d" % i, "raw_title": variants[i % 3], "raw_description": "Pack of 2"}
                 for i in range(30)]

        job = self.wait_for_job(pipeline.submit_job(items)["id"])

        self.assertEqual(provider.calls, 1)
        self.assertEqual((job["done"], job["failed"], job["cache_hits"]), (30, 0, 29))
        titles = {self.db.get_product(item["sku"])["clean_title"] for item in items}
        self.assertEqual(len(titles), 1)

    def test_duplicates_in_two_jobs_at_the_same_moment_make_one_call(self):
        provider = CountingMock(latency_ms=150, failure_rate=0)
        pipeline = self.make_pipeline(provider, concurrency=5)
        first = pipeline.submit_job([{"sku": "A", "raw_title": "Tata Salt 1kg", "raw_description": ""}])
        second = pipeline.submit_job([{"sku": "B", "raw_title": "tata salt 1KG", "raw_description": ""}])

        hits = sum(self.wait_for_job(job["id"])["cache_hits"] for job in (first, second))

        self.assertEqual(provider.calls, 1)
        self.assertEqual(hits, 1)

    def test_content_enriched_earlier_is_not_sent_again(self):
        provider = CountingMock(latency_ms=0, failure_rate=0)
        pipeline = self.make_pipeline(provider)
        self.wait_for_job(pipeline.submit_job(products(10))["id"])
        self.assertEqual(provider.calls, 10)

        job = self.wait_for_job(pipeline.submit_job(products(10, prefix="NEW"))["id"])

        self.assertEqual(provider.calls, 10)
        self.assertEqual((job["done"], job["cache_hits"]), (10, 10))
        self.assertEqual(self.db.get_product("NEW-0003")["status"], "enriched")

    def test_cache_survives_a_restart(self):
        first = self.make_pipeline(CountingMock(latency_ms=0, failure_rate=0))
        self.wait_for_job(first.submit_job(products(5))["id"])
        first.stop()

        provider = CountingMock(latency_ms=0, failure_rate=0)
        reopened = Database(self.db_path)
        second = self.make_pipeline(provider, db=reopened)
        job = self.wait_for_job(second.submit_job(products(5, prefix="AGAIN"))["id"], db=reopened)

        self.assertEqual(provider.calls, 0)
        self.assertEqual(job["cache_hits"], 5)

    def test_concurrent_duplicates_of_failing_content_share_the_failure(self):
        provider = CountingMock(latency_ms=20, failure_rate=1.0)
        pipeline = self.make_pipeline(provider, concurrency=5, retry_base_ms=1)
        items = [{"sku": "F-%d" % i, "raw_title": "same thing", "raw_description": ""}
                 for i in range(6)]

        job = self.wait_for_job(pipeline.submit_job(items)["id"])

        self.assertEqual(provider.calls, 4)  # one product's worth of attempts, not six
        self.assertEqual((job["done"], job["failed"], job["cache_hits"]), (0, 6, 0))

    def test_failures_are_not_cached(self):
        provider = CountingMock(latency_ms=0, failure_rate=1.0)
        pipeline = self.make_pipeline(provider, retry_base_ms=1)
        self.wait_for_job(pipeline.submit_job(products(1))["id"])
        self.assertEqual(self.db.get_product("SKU-0000")["status"], "failed")

        provider.failure_rate = 0
        job = self.wait_for_job(pipeline.submit_job(products(1))["id"])

        self.assertEqual((job["done"], job["cache_hits"]), (1, 0))
        self.assertEqual(self.db.get_product("SKU-0000")["status"], "enriched")


class RecoveryTests(PipelineTestCase):
    def test_unfinished_items_resume_after_a_restart(self):
        # A pipeline that is never started stands in for a server that crashed
        # right after accepting the job.
        crashed = self.make_pipeline(MockProvider(0, 0), start=False)
        job_id = crashed.submit_job(products(8))["id"]
        self.assertEqual(self.db.get_job(job_id)["status"], "queued")

        reopened = Database(self.db_path)
        self.make_pipeline(CountingMock(latency_ms=0, failure_rate=0), db=reopened)
        job = self.wait_for_job(job_id, db=reopened)

        self.assertEqual((job["total"], job["done"], job["failed"]), (8, 8, 0))

    def test_restart_does_not_redo_finished_items(self):
        first = self.make_pipeline(CountingMock(latency_ms=0, failure_rate=0))
        job_id = first.submit_job(products(6))["id"]
        self.wait_for_job(job_id)
        first.stop()

        provider = CountingMock(latency_ms=0, failure_rate=0)
        reopened = Database(self.db_path)
        self.make_pipeline(provider, db=reopened)
        time.sleep(0.1)

        self.assertEqual(provider.calls, 0)
        self.assertEqual(reopened.get_job(job_id)["done"], 6)


if __name__ == "__main__":
    unittest.main()
