"""Entry point: python -m catalogiq"""
import os
import sys

from .config import from_env
from .db import Database
from .llm import build_provider
from .pipeline import Pipeline
from .server import make_server


def _load_dotenv(path=".env"):
    """Load key=value pairs from a .env file into os.environ (no overwrite)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip("'\"")
    except FileNotFoundError:
        pass


def main():
    _load_dotenv()
    try:
        cfg = from_env()
        provider = build_provider(cfg, os.environ)
    except ValueError as exc:
        sys.exit("Configuration error: %s" % exc)

    db = Database(cfg.db_path)
    pipeline = Pipeline(db, provider, cfg.llm_concurrency, cfg.retry_base_ms)
    resumed = len(db.pending_item_ids())
    pipeline.start()
    server = make_server(cfg, db, pipeline)

    print("CatalogIQ on http://localhost:%d  (provider=%s, concurrency=%d, db=%s)" % (
        cfg.port, cfg.llm_provider, cfg.llm_concurrency, cfg.db_path), flush=True)
    if resumed:
        print("Resuming %d unfinished item(s) from before the restart" % resumed, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
