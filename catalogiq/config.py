"""Runtime configuration, read once from environment variables at startup."""
import os
from dataclasses import dataclass

CATEGORIES = [
    "Groceries",
    "Beverages",
    "Personal Care",
    "Household",
    "Electronics",
    "Fashion",
    "Home & Kitchen",
    "Other",
]

# One LLM call plus up to 3 retries.
MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class Config:
    llm_provider: str = "mock"
    llm_concurrency: int = 5
    mock_latency_ms: int = 200
    mock_failure_rate: float = 0.1
    retry_base_ms: int = 200
    llm_model: str = ""
    llm_base_url: str = ""
    llm_timeout_s: float = 30.0
    db_path: str = "catalogiq.db"
    host: str = "127.0.0.1"
    port: int = 8000


def _int(env, name, default, minimum):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("%s must be an integer, got %r" % (name, raw))
    if value < minimum:
        raise ValueError("%s must be >= %d, got %d" % (name, minimum, value))
    return value


def _float(env, name, default, minimum, maximum):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("%s must be a number, got %r" % (name, raw))
    if not minimum <= value <= maximum:
        raise ValueError("%s must be between %s and %s" % (name, minimum, maximum))
    return value


def from_env(env=None):
    env = os.environ if env is None else env
    return Config(
        llm_provider=(env.get("LLM_PROVIDER") or "mock").strip().lower(),
        llm_concurrency=_int(env, "LLM_CONCURRENCY", 5, 1),
        mock_latency_ms=_int(env, "MOCK_LATENCY_MS", 200, 0),
        mock_failure_rate=_float(env, "MOCK_FAILURE_RATE", 0.1, 0.0, 1.0),
        retry_base_ms=_int(env, "RETRY_BASE_MS", 200, 0),
        llm_model=(env.get("LLM_MODEL") or "").strip(),
        llm_base_url=(env.get("LLM_BASE_URL") or "").strip(),
        llm_timeout_s=_float(env, "LLM_TIMEOUT_S", 30.0, 1.0, 600.0),
        db_path=env.get("DB_PATH") or "catalogiq.db",
        host=env.get("HOST") or "127.0.0.1",
        port=_int(env, "PORT", 8000, 1),
    )
