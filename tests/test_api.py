"""API contract tests against a real server on a random local port."""
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from catalogiq.config import Config
from catalogiq.llm import MockProvider
from catalogiq.server import make_server

from .helpers import PipelineTestCase, products


class ApiTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = Config(llm_provider="mock", llm_concurrency=5, port=0)
        self.pipeline = self.make_pipeline(MockProvider(latency_ms=5, failure_rate=0), 5)
        self.server = make_server(self.cfg, self.db, self.pipeline)
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def run_job(self, items):
        status, job = self.call("POST", "/api/jobs", {"products": items})
        self.assertEqual(status, 202)
        return self.wait_for_job(job["id"])

    def assert_error(self, response, status):
        self.assertEqual(response[0], status)
        self.assertEqual(list(response[1]), ["error"])
        self.assertIsInstance(response[1]["error"], str)

    # ---- health and metrics ----------------------------------------------

    def test_health(self):
        self.assertEqual(self.call("GET", "/api/health"), (200, {
            "status": "ok", "llm_provider": "mock", "llm_concurrency": 5}))

    def test_metrics_are_integers(self):
        self.run_job(products(12))
        status, metrics = self.call("GET", "/api/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(set(metrics), {
            "llm_calls_total", "llm_errors_total", "max_concurrent_llm_calls"})
        self.assertTrue(all(type(value) is int for value in metrics.values()))
        self.assertEqual(metrics["llm_calls_total"], 12)
        self.assertLessEqual(metrics["max_concurrent_llm_calls"], 5)

    # ---- jobs ------------------------------------------------------------

    def test_submit_job_returns_202_quickly_with_the_job_object(self):
        started = time.time()
        status, job = self.call("POST", "/api/jobs", {"products": products(2000)})
        elapsed = time.time() - started

        self.assertEqual(status, 202)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(set(job), {"id", "status", "total", "done", "failed", "cache_hits",
                                    "created_at", "started_at", "finished_at"})
        self.assertEqual(job["total"], 2000)
        self.assertIn(job["status"], ("queued", "running"))
        self.assertIsNone(job["finished_at"])

    def test_api_stays_fast_while_a_job_runs(self):
        self.call("POST", "/api/jobs", {"products": products(2000)})
        slowest = 0
        for _ in range(20):
            started = time.time()
            self.assertEqual(self.call("GET", "/api/health")[0], 200)
            self.assertEqual(self.call("GET", "/api/products?q=product")[0], 200)
            slowest = max(slowest, time.time() - started)
        self.assertLess(slowest, 0.5)

    def test_job_progress_and_completion(self):
        status, job = self.call("POST", "/api/jobs", {"products": products(30)})
        status, live = self.call("GET", "/api/jobs/" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(live["total"], 30)

        self.wait_for_job(job["id"])
        status, finished = self.call("GET", "/api/jobs/" + job["id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual((finished["done"], finished["failed"]), (30, 0))
        self.assertIsNotNone(finished["started_at"])
        self.assertIsNotNone(finished["finished_at"])

    def test_submit_job_rejects_bad_input(self):
        bad_bodies = [
            {"products": []},
            {},
            {"products": "nope"},
            {"products": [{"raw_title": "no sku"}]},
            {"products": [{"sku": "A1"}]},
            {"products": [{"sku": "  ", "raw_title": "blank sku"}]},
            {"products": [{"sku": "A1", "raw_title": "   "}]},
            {"products": [{"sku": "A1", "raw_title": "ok"}, {"sku": "A2"}]},
        ]
        for body in bad_bodies:
            self.assert_error(self.call("POST", "/api/jobs", body), 400)
        self.assert_error(self.call("POST", "/api/jobs", raw=b"{not json"), 400)
        # A rejected job stores nothing.
        self.assertEqual(self.call("GET", "/api/products")[1]["total"], 0)

    def test_unknown_job_is_404(self):
        self.assert_error(self.call("GET", "/api/jobs/j_missing"), 404)

    def test_description_is_optional(self):
        job = self.run_job([{"sku": "A1", "raw_title": "Amul butter 500g"}])
        self.assertEqual(job["done"], 1)
        self.assertEqual(self.call("GET", "/api/products/A1")[1]["raw_description"], "")

    # ---- products --------------------------------------------------------

    def seed(self):
        self.run_job([
            {"sku": "C3", "raw_title": "TATA tea gold 250g", "raw_description": ""},
            {"sku": "A1", "raw_title": " AMUL  butter 500G", "raw_description": "pck of 2"},
            {"sku": "B2", "raw_title": "dove soap 100g", "raw_description": "bathing bar"},
            {"sku": "D4", "raw_title": "amul cheese slices 200g", "raw_description": ""},
        ])

    def test_product_object(self):
        self.seed()
        status, product = self.call("GET", "/api/products/A1")
        self.assertEqual(status, 200)
        self.assertEqual(product, {
            "sku": "A1", "raw_title": " AMUL  butter 500G", "raw_description": "pck of 2",
            "clean_title": "Amul Butter 500 g (Pack of 2)", "category": "Groceries",
            "brand": "Amul", "tags": ["butter"], "status": "enriched", "error": None})

    def test_list_is_sorted_by_sku_and_paginated(self):
        self.seed()
        status, page = self.call("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertEqual([p["sku"] for p in page["items"]], ["A1", "B2", "C3", "D4"])
        self.assertEqual((page["page"], page["page_size"], page["total"]), (1, 20, 4))

        status, page = self.call("GET", "/api/products?page=2&page_size=3")
        self.assertEqual([p["sku"] for p in page["items"]], ["D4"])
        self.assertEqual((page["page"], page["page_size"], page["total"]), (2, 3, 4))

        self.assertEqual(self.call("GET", "/api/products?page=9")[1]["items"], [])
        self.assertEqual(self.call("GET", "/api/products?page_size=500")[1]["page_size"], 100)

    def test_list_rejects_non_numeric_paging(self):
        for query in ("page=abc", "page_size=ten", "page=1.5", "page=", "page=0", "page_size=-1"):
            self.assert_error(self.call("GET", "/api/products?" + query), 400)

    def test_category_filter_and_search(self):
        self.seed()
        skus = lambda path: [p["sku"] for p in self.call("GET", path)[1]["items"]]

        self.assertEqual(skus("/api/products?category=Groceries"), ["A1", "D4"])
        self.assertEqual(skus("/api/products?category=" + quote("Personal Care")), ["B2"])
        self.assertEqual(skus("/api/products?category=groceries"), [])  # exact match
        self.assertEqual(skus("/api/products?q=AMUL"), ["A1", "D4"])  # case-insensitive
        self.assertEqual(skus("/api/products?q=amul&category=Groceries&page_size=1"), ["A1"])
        self.assertEqual(skus("/api/products?q=" + quote("Pack of 2")), ["A1"])  # clean title
        self.assertEqual(skus("/api/products?q=" + quote("100%")), [])  # % is not a wildcard

    def test_unknown_sku_is_404(self):
        self.assert_error(self.call("GET", "/api/products/NOPE"), 404)
        self.assert_error(self.call("PATCH", "/api/products/NOPE", {"clean_title": "x"}), 404)

    def test_patch_edits_and_approves(self):
        self.seed()
        status, product = self.call("PATCH", "/api/products/B2", {
            "clean_title": "Dove Beauty Bar 100 g", "category": "Personal Care",
            "tags": ["Soap", "bath"]})
        self.assertEqual(status, 200)
        self.assertEqual(product["clean_title"], "Dove Beauty Bar 100 g")
        self.assertEqual(product["tags"], ["soap", "bath"])
        self.assertEqual(product["status"], "approved")
        self.assertEqual(self.call("GET", "/api/products/B2")[1], product)

        status, product = self.call("PATCH", "/api/products/C3", {"category": "Groceries"})
        self.assertEqual((product["category"], product["status"]), ("Groceries", "approved"))
        self.assertEqual(product["clean_title"], "Tata Tea Gold 250 g")  # untouched

    def test_patch_rejects_bad_input(self):
        self.seed()
        bad_bodies = [{"category": "Toys"}, {"clean_title": ""}, {"clean_title": "   "},
                      {"tags": "soap"}, {"tags": ["a", "b", "c", "d", "e", "f"]}]
        for body in bad_bodies:
            self.assert_error(self.call("PATCH", "/api/products/B2", body), 400)
        self.assertEqual(self.call("GET", "/api/products/B2")[1]["status"], "enriched")

    def test_resubmitting_a_sku_updates_it(self):
        self.seed()
        self.call("PATCH", "/api/products/A1", {"clean_title": "Edited"})

        self.run_job([{"sku": "A1", "raw_title": "nescafe coffee 50g", "raw_description": ""}])

        product = self.call("GET", "/api/products/A1")[1]
        self.assertEqual(product["raw_title"], "nescafe coffee 50g")
        self.assertEqual(product["category"], "Beverages")
        self.assertEqual(product["status"], "enriched")
        self.assertEqual(self.call("GET", "/api/products")[1]["total"], 4)

    def test_failed_product_shows_its_error(self):
        self.pipeline.provider.failure_rate = 1.0
        self.pipeline.retry_base_ms = 1
        job = self.run_job([{"sku": "X1", "raw_title": "anything"}])

        self.assertEqual(job["failed"], 1)
        product = self.call("GET", "/api/products/X1")[1]
        self.assertEqual(product["status"], "failed")
        self.assertTrue(product["error"])

    # ---- misc ------------------------------------------------------------

    def test_unknown_route_and_wrong_method_return_json_errors(self):
        self.assert_error(self.call("GET", "/api/nope"), 404)
        self.assert_error(self.call("DELETE", "/api/products/A1"), 405)
        self.assert_error(self.call("GET", "/api/jobs"), 405)

    def test_frontend_is_served_at_root(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn(b"CatalogIQ", response.read())
