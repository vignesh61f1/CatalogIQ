"""SQLite storage: products, jobs, job items and the enrichment cache.

Each thread gets its own connection (SQLite connections are not meant to be
shared). WAL mode lets API reads run while a worker is writing, and a single
in-process lock serialises writers so they never hit "database is locked".
"""
import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku             TEXT PRIMARY KEY,
    raw_title       TEXT NOT NULL,
    raw_description TEXT NOT NULL DEFAULT '',
    content_key     TEXT NOT NULL,
    clean_title     TEXT,
    category        TEXT,
    brand           TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category, sku);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    total       INTEGER NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    cache_hits  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS job_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(id),
    sku             TEXT NOT NULL,
    raw_title       TEXT NOT NULL,
    raw_description TEXT NOT NULL,
    content_key     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_items_state ON job_items(state, id);

CREATE TABLE IF NOT EXISTS enrichment_cache (
    content_key TEXT PRIMARY KEY,
    clean_title TEXT NOT NULL,
    category    TEXT NOT NULL,
    brand       TEXT,
    tags        TEXT NOT NULL
);
"""

JOB_COLUMNS = "id, status, total, done, failed, cache_hits, created_at, started_at, finished_at"
PRODUCT_COLUMNS = ("sku, raw_title, raw_description, clean_title, category, brand, tags, "
                   "status, error")


def _product(row):
    if row is None:
        return None
    product = dict(row)
    product["tags"] = json.loads(product["tags"])
    return product


def _like_pattern(text):
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # isolation_level=None: we issue BEGIN/COMMIT ourselves.
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close_current(self):
        """Close the calling thread's connection (called when a thread finishes)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _write(self):
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ---- jobs ------------------------------------------------------------

    def create_job(self, job_id, items, now):
        """Store a job, its items and the raw products in one transaction.

        ``items`` is a list of (sku, raw_title, raw_description, content_key).
        Returns the new job_items ids, in submission order.
        """
        with self._write() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, total, created_at) VALUES (?, 'queued', ?, ?)",
                (job_id, len(items), now))
            conn.executemany(
                "INSERT INTO products (sku, raw_title, raw_description, content_key, status) "
                "VALUES (?, ?, ?, ?, 'pending') "
                "ON CONFLICT(sku) DO UPDATE SET raw_title = excluded.raw_title, "
                "raw_description = excluded.raw_description, content_key = excluded.content_key, "
                "clean_title = NULL, category = NULL, brand = NULL, tags = '[]', "
                "status = 'pending', error = NULL",
                items)
            conn.executemany(
                "INSERT INTO job_items (job_id, sku, raw_title, raw_description, content_key) "
                "VALUES (?, ?, ?, ?, ?)",
                [(job_id,) + tuple(item) for item in items])
            rows = conn.execute(
                "SELECT id FROM job_items WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        return [row["id"] for row in rows]

    def get_job(self, job_id):
        row = self._conn().execute(
            "SELECT %s FROM jobs WHERE id = ?" % JOB_COLUMNS, (job_id,)).fetchone()
        return dict(row) if row else None

    def mark_job_running(self, job_id, now):
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'queued'", (now, job_id))

    def get_item(self, item_id):
        row = self._conn().execute(
            "SELECT id, job_id, sku, raw_title, raw_description, content_key, state "
            "FROM job_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def pending_item_ids(self):
        """Items that were accepted but not finished (used to resume after a restart)."""
        rows = self._conn().execute(
            "SELECT id FROM job_items WHERE state = 'pending' ORDER BY id").fetchall()
        return [row["id"] for row in rows]

    def finish_item(self, item, now, result=None, error=None, cache_hit=False,
                    store_in_cache=False):
        """Record one item's outcome. Everything happens in one transaction, so a
        crash can never leave an item counted twice or a job counter out of step."""
        ok = result is not None
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE job_items SET state = ?, error = ? WHERE id = ? AND state = 'pending'",
                ("done" if ok else "failed", error, item["id"])).rowcount
            if not changed:
                return  # already recorded
            if ok and store_in_cache:
                conn.execute(
                    "INSERT OR REPLACE INTO enrichment_cache "
                    "(content_key, clean_title, category, brand, tags) VALUES (?, ?, ?, ?, ?)",
                    (item["content_key"], result["clean_title"], result["category"],
                     result["brand"], json.dumps(result["tags"])))
            # Only touch the product if it still holds this content and is still
            # waiting: a newer upload or a human edit must not be overwritten.
            guard = "WHERE sku = ? AND content_key = ? AND status = 'pending'"
            if ok:
                conn.execute(
                    "UPDATE products SET clean_title = ?, category = ?, brand = ?, tags = ?, "
                    "status = 'enriched', error = NULL " + guard,
                    (result["clean_title"], result["category"], result["brand"],
                     json.dumps(result["tags"]), item["sku"], item["content_key"]))
            else:
                conn.execute(
                    "UPDATE products SET status = 'failed', error = ? " + guard,
                    (error, item["sku"], item["content_key"]))
            conn.execute(
                "UPDATE jobs SET done = done + ?, failed = failed + ?, "
                "cache_hits = cache_hits + ? WHERE id = ?",
                (1 if ok else 0, 0 if ok else 1, 1 if (ok and cache_hit) else 0,
                 item["job_id"]))
            conn.execute(
                "UPDATE jobs SET status = 'completed', finished_at = ?, "
                "started_at = COALESCE(started_at, ?) "
                "WHERE id = ? AND status != 'completed' AND done + failed >= total",
                (now, now, item["job_id"]))

    # ---- cache -----------------------------------------------------------

    def cache_get(self, content_key):
        row = self._conn().execute(
            "SELECT clean_title, category, brand, tags FROM enrichment_cache "
            "WHERE content_key = ?", (content_key,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["tags"] = json.loads(result["tags"])
        return result

    # ---- products --------------------------------------------------------

    def get_product(self, sku):
        return _product(self._conn().execute(
            "SELECT %s FROM products WHERE sku = ?" % PRODUCT_COLUMNS, (sku,)).fetchone())

    def list_products(self, page, page_size, category=None, q=None, status=None):
        where, params = [], []
        if category:
            where.append("category = ?")
            params.append(category)
        if status:
            where.append("status = ?")
            params.append(status)
        if q:
            where.append("(clean_title LIKE ? ESCAPE '\\' OR raw_title LIKE ? ESCAPE '\\')")
            params += [_like_pattern(q)] * 2
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM products" + clause, params).fetchone()[0]
        rows = conn.execute(
            "SELECT %s FROM products%s ORDER BY sku LIMIT ? OFFSET ?" % (PRODUCT_COLUMNS, clause),
            params + [page_size, (page - 1) * page_size]).fetchall()
        return {"items": [_product(row) for row in rows], "page": page,
                "page_size": page_size, "total": total}

    def approve_product(self, sku, changes):
        """Apply human edits (clean_title, category, tags) and mark the product approved."""
        sets, params = ["status = 'approved'", "error = NULL"], []
        for column in ("clean_title", "category"):
            if column in changes:
                sets.append(column + " = ?")
                params.append(changes[column])
        if "tags" in changes:
            sets.append("tags = ?")
            params.append(json.dumps(changes["tags"]))
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE products SET %s WHERE sku = ?" % ", ".join(sets), params + [sku]).rowcount
        return self.get_product(sku) if changed else None
