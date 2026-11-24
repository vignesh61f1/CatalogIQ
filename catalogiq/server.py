"""HTTP API and static frontend, on the standard library's threaded HTTP server.

Each request runs in its own thread, so slow requests never block others and
the API stays responsive while the pipeline workers are busy.
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .config import CATEGORIES

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY_BYTES = 50 * 1024 * 1024
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_TAGS = 5


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def validate_products(body):
    """Check a POST /api/jobs body and return the cleaned product list."""
    if not isinstance(body, dict) or not isinstance(body.get("products"), list):
        raise ApiError(400, 'body must be a JSON object like {"products": [...]}')
    if not body["products"]:
        raise ApiError(400, "products must not be empty")
    cleaned = []
    for index, product in enumerate(body["products"]):
        if not isinstance(product, dict):
            raise ApiError(400, "products[%d] must be an object" % index)
        sku = product.get("sku")
        if isinstance(sku, (int, float)) and not isinstance(sku, bool):
            sku = str(sku)
        if not isinstance(sku, str) or not sku.strip():
            raise ApiError(400, "products[%d] has no sku" % index)
        title = product.get("raw_title")
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, "products[%d] has no raw_title" % index)
        description = product.get("raw_description")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise ApiError(400, "products[%d].raw_description must be a string" % index)
        cleaned.append({"sku": sku.strip(), "raw_title": title, "raw_description": description})
    return cleaned


def validate_patch(body):
    """Check a PATCH /api/products/<sku> body and return the fields to change."""
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    changes = {}
    if "clean_title" in body:
        title = body["clean_title"]
        if not isinstance(title, str) or not title.strip():
            raise ApiError(400, "clean_title must not be empty")
        changes["clean_title"] = title.strip()
    if "category" in body:
        if body["category"] not in CATEGORIES:
            raise ApiError(400, "category must be one of: " + ", ".join(CATEGORIES))
        changes["category"] = body["category"]
    if "tags" in body:
        tags = body["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ApiError(400, "tags must be a list of strings")
        cleaned = []
        for tag in tags:
            tag = tag.strip().lower()
            if tag and tag not in cleaned:
                cleaned.append(tag)
        if len(cleaned) > MAX_TAGS:
            raise ApiError(400, "at most %d tags are allowed" % MAX_TAGS)
        changes["tags"] = cleaned
    return changes


def _positive_int(query, name, default):
    if name not in query:
        return default
    raw = query[name][0].strip()
    if not re.fullmatch(r"\d+", raw) or int(raw) < 1:
        raise ApiError(400, "%s must be a positive integer" % name)
    return int(raw)


def make_handler(cfg, db, pipeline):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CatalogIQ/1.0"
        protocol_version = "HTTP/1.1"

        # ---- plumbing ----------------------------------------------------

        def log_message(self, format, *args):
            if os.environ.get("ACCESS_LOG"):
                super().log_message(format, *args)

        def finish(self):
            super().finish()
            db.close_current()  # this request thread is done with its connection

        def _send(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status, payload):
            self._send(status, json.dumps(payload).encode("utf-8"))

        def _read_body(self):
            # Always drain the body, even if the request turns out to be invalid,
            # so a kept-alive connection stays in sync for the next request.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                raise ApiError(413 if length > 0 else 400, "invalid or too large request body")
            return self.rfile.read(length) if length else b""

        def _read_json(self):
            try:
                return json.loads(self._body.decode("utf-8"))
            except ValueError:
                raise ApiError(400, "request body is not valid JSON")

        def _handle(self, method):
            try:
                self._body = self._read_body()
                url = urlsplit(self.path)
                path = url.path if url.path == "/" else url.path.rstrip("/")
                query = parse_qs(url.query, keep_blank_values=True)
                self._route(method, path, query)
            except ApiError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self._send_json(500, {"error": "internal server error: %s" % exc})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

        # ---- routing -----------------------------------------------------

        def _route(self, method, path, query):
            if method == "GET" and path in STATIC_FILES:
                return self._static(path)

            routes = [
                (r"/api/health", {"GET": self._health}),
                (r"/api/metrics", {"GET": self._metrics}),
                (r"/api/jobs", {"POST": self._create_job}),
                (r"/api/jobs/([^/]+)", {"GET": self._get_job}),
                (r"/api/products", {"GET": self._list_products}),
                (r"/api/products/(.+)", {"GET": self._get_product, "PATCH": self._patch_product}),
            ]
            for pattern, methods in routes:
                match = re.fullmatch(pattern, path)
                if not match:
                    continue
                if method not in methods:
                    raise ApiError(405, "method %s is not allowed on %s" % (method, path))
                return methods[method](query, *[unquote(g) for g in match.groups()])
            raise ApiError(404, "not found: %s" % path)

        def _static(self, path):
            filename, content_type = STATIC_FILES[path]
            with open(os.path.join(STATIC_DIR, filename), "rb") as handle:
                self._send(200, handle.read(), content_type)

        # ---- endpoints ---------------------------------------------------

        def _health(self, query):
            self._send_json(200, {
                "status": "ok",
                "llm_provider": cfg.llm_provider,
                "llm_concurrency": cfg.llm_concurrency,
            })

        def _metrics(self, query):
            self._send_json(200, pipeline.metrics.snapshot())

        def _create_job(self, query):
            products = validate_products(self._read_json())
            self._send_json(202, pipeline.submit_job(products))

        def _get_job(self, query, job_id):
            job = db.get_job(job_id)
            if job is None:
                raise ApiError(404, "unknown job: %s" % job_id)
            self._send_json(200, job)

        def _list_products(self, query):
            page = _positive_int(query, "page", 1)
            page_size = min(_positive_int(query, "page_size", DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
            self._send_json(200, db.list_products(
                page, page_size,
                category=query.get("category", [""])[0] or None,
                q=query.get("q", [""])[0].strip() or None,
                status=query.get("status", [""])[0] or None))

        def _get_product(self, query, sku):
            product = db.get_product(sku)
            if product is None:
                raise ApiError(404, "unknown SKU: %s" % sku)
            self._send_json(200, product)

        def _patch_product(self, query, sku):
            if db.get_product(sku) is None:
                raise ApiError(404, "unknown SKU: %s" % sku)
            changes = validate_patch(self._read_json())
            self._send_json(200, db.approve_product(sku, changes))

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def make_server(cfg, db, pipeline):
    return Server((cfg.host, cfg.port), make_handler(cfg, db, pipeline))
