# CatalogIQ

Messy product listings in, clean searchable catalogue out. An LLM cleans up each
listing (title, category, brand, tags) and the pipeline handles concurrency,
retries, caching and failures around it.

- **Backend:** Python stdlib only (`http.server`, `threading`, `sqlite3`). Zero dependencies.
- **Frontend:** plain HTML/CSS/JS at `/`.
- **Storage:** SQLite (`catalogiq.db`), survives restarts.
- **Real LLM:** Groq free tier (`llama-3.1-8b-instant`). Ollama, Gemini and OpenRouter also work.

Design writeup and architecture diagram: [DESIGN.md](DESIGN.md)

## Setup

Python 3.9+. No pip install needed.

```bash
git clone https://github.com/vignesh61f1/CatalogIQ.git
cd CatalogIQ
python -m catalogiq
```

Open http://localhost:8000, upload `data/sample_listings.csv` (250 rows, 40 duplicates).

## Mock mode (default)

```bash
python -m catalogiq
```

Mock waits `MOCK_LATENCY_MS`, fails randomly at `MOCK_FAILURE_RATE`, otherwise
uses keyword rules for category/brand/tags.

## Real LLM

```bash
export GROQ_API_KEY=...
LLM_PROVIDER=groq python -m catalogiq
```

Windows PowerShell:
```powershell
$env:GROQ_API_KEY="..."
$env:LLM_PROVIDER="groq"
python -m catalogiq
```

Other providers (all OpenAI-compatible):

| `LLM_PROVIDER` | Key variable | Default model |
|---|---|---|
| `groq` | `GROQ_API_KEY` | `llama-3.1-8b-instant` |
| `ollama` | — (run `ollama pull llama3.2:3b`) | `llama3.2:3b` |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.0-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct:free` |

Free-tier models rotate; override with `LLM_MODEL` if a default is gone. With
Groq, `LLM_CONCURRENCY=2` avoids most 429s (and the 429 `Retry-After` header
is respected).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock`, `groq`, `ollama`, `gemini` or `openrouter` |
| `LLM_CONCURRENCY` | `5` | Max LLM calls in flight at once, across all jobs |
| `MOCK_LATENCY_MS` | `200` | Mock call latency |
| `MOCK_FAILURE_RATE` | `0.1` | Chance (0–1) a mock call fails |
| `LLM_MODEL` | per provider | Override model name |
| `LLM_BASE_URL` | per provider | Override API endpoint |
| `LLM_TIMEOUT_S` | `30` | Timeout for one real LLM call |
| `RETRY_BASE_MS` | `200` | Initial retry delay; doubles each retry |
| `DB_PATH` | `catalogiq.db` | SQLite file path |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Listen address |
| `ACCESS_LOG` | unset | Set to `1` for per-request logging |

## Tests

```bash
python -m unittest              # all tests, ~15s on mock
python -m unittest -v tests.test_pipeline   # just concurrency/retries/dedup
```

What's tested:
- `tests/test_pipeline.py` — concurrency limit, retries + backoff, dedup (same-moment and cross-job), crash recovery
- `tests/test_api.py` — every API endpoint against a real server on a random port
- `tests/test_llm.py` — mock provider, validation, real provider client against a local stand-in

## Project layout

| File | What |
|---|---|
| `catalogiq/pipeline.py` | Workers, concurrency semaphore, retries, dedup. **Start here.** |
| `catalogiq/llm.py` | Mock + real providers, prompt, output validation |
| `catalogiq/db.py` | SQLite schema and queries |
| `catalogiq/server.py` | HTTP API + input validation |
| `catalogiq/metrics.py` | Thread-safe counters |
| `catalogiq/config.py` | Env var parsing |
| `static/` | Frontend (single page) |
| `scripts/generate_sample.py` | Regenerates `data/sample_listings.csv` |

## The prompt

System message (in `catalogiq/llm.py`):

```
You clean up messy e-commerce product listings.
Reply with one JSON object and nothing else, with exactly these keys:
- "clean_title": a tidy, human-readable product title (fix casing, spacing and units, e.g. "500 g")
- "category": exactly one of: Groceries, Beverages, Personal Care, Household, Electronics, Fashion, Home & Kitchen, Other
- "brand": the brand name as a string, or null if it is not stated in the listing. Never guess a brand.
- "tags": a list of up to 5 short lowercase search tags
Use only information present in the listing.
```

User message: `Raw title: <title>\nRaw description: <description or "(none)">`

Also sets `temperature: 0` and `response_format: {"type": "json_object"}`.

## Assumptions

- **`done` counts successes only.** `completed` means `done + failed == total`. Cache hits count as `done`.
- **Products appear in the catalogue immediately** with status `pending`, flip to `enriched`/`failed` when processed. This also makes jobs resumable after a crash.
- **Validation failures count as LLM errors** in `llm_errors_total`, same as provider errors. Both use up one of the 4 attempts.
- **Only hard failures reject an attempt:** invalid JSON, category outside the list, missing title, wrong types. Fixable stuff gets fixed silently: tags lowercased/deduped/trimmed to 5, blank brand → `null`.
- **Concurrent duplicates share the outcome.** If the first copy succeeds, the rest are cache hits. If it fails all 4 attempts, they all fail too — no extra LLM calls. Failures aren't cached, so a later job retries.
- **Resubmitting a SKU re-enriches it,** even if approved.
- **`page_size` over 100 caps at 100** (not 400). `page=0` or negative → 400.
- **Empty-body `PATCH` approves unchanged.** More than 5 tags → 400.
- **`GET /api/products` also accepts `status=`** for the UI's status filter.
- **Same SKU twice in one job:** both rows processed and counted; last one wins in the catalogue.

## What's unfinished

- **Real LLM not tested end-to-end in the test suite.** Client is tested against a local stand-in (request format, 429 handling, network errors). Run with your own key to verify.
- **Frontend tested manually**, not with a headless browser. 360px layout and keyboard nav checked by hand.
- **Metrics reset on restart** — kept in memory only.
- **A crash can repeat an in-flight LLM call** (never a finished one, at most `LLM_CONCURRENCY` of them).
- **No job list endpoint, cancel, or auth.**
- **Search is `LIKE '%q%'`** — fine at current scale, DESIGN.md covers the 5M row plan.
- **Prompt batching** (multiple products per LLM call) described in DESIGN.md but not built.

## AI tools

Used Claude Code for drafting most of the code, tests and docs from the assignment brief.
I wrote the design decisions, reviewed everything, ran the tests, did a full 250-listing
run in mock mode, and tested crash recovery with a kill mid-job + restart. The sample data
script and category keyword lists I tweaked by hand after the initial generation.
