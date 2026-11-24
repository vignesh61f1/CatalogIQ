"""LLM providers and output validation.

Every provider exposes the same interface: ``enrich(raw_title, raw_description)``
returns the model's raw text. The pipeline validates that text with
``parse_enrichment``, so the mock and the real provider go through the same
checks and switching between them is only a config change.
"""
import json
import random
import re
import time
import urllib.error
import urllib.request

from .config import CATEGORIES

SYSTEM_PROMPT = """You clean up messy e-commerce product listings.
Reply with one JSON object and nothing else, with exactly these keys:
- "clean_title": a tidy, human-readable product title (fix casing, spacing and units, e.g. "500 g")
- "category": exactly one of: Groceries, Beverages, Personal Care, Household, Electronics, Fashion, Home & Kitchen, Other
- "brand": the brand name as a string, or null if it is not stated in the listing. Never guess a brand.
- "tags": a list of up to 5 short lowercase search tags
Use only information present in the listing."""

USER_PROMPT = "Raw title: {title}\nRaw description: {description}"


class LLMError(Exception):
    """One failed LLM attempt (provider error or invalid output)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class ValidationError(LLMError):
    """The LLM answered, but the answer is not usable."""


def parse_enrichment(text):
    """Validate raw LLM text and return a normalised enrichment dict."""
    if not isinstance(text, str):
        raise ValidationError("LLM output is not text")
    body = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
    if fenced:
        body = fenced.group(1)
    try:
        data = json.loads(body)
    except ValueError:
        raise ValidationError("LLM output is not valid JSON")
    if not isinstance(data, dict):
        raise ValidationError("LLM output is not a JSON object")

    title = data.get("clean_title")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("clean_title is missing or empty")

    category = data.get("category")
    if category not in CATEGORIES:
        raise ValidationError("category %r is not in the allowed list" % (category,))

    brand = data.get("brand")
    if brand is not None and not isinstance(brand, str):
        raise ValidationError("brand must be a string or null")
    if isinstance(brand, str):
        brand = brand.strip() or None

    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        raise ValidationError("tags must be a list of strings")
    tags = []
    for tag in raw_tags:
        tag = tag.strip().lower()
        if tag and tag not in tags:
            tags.append(tag)

    return {
        "clean_title": title.strip(),
        "category": category,
        "brand": brand,
        "tags": tags[:5],
    }


# --------------------------------------------------------------------------
# Mock provider
# --------------------------------------------------------------------------

# Checked in this order; the first category with a matching word wins.
_CATEGORY_KEYWORDS = [
    ("Electronics", {
        "earphones", "earbuds", "headphones", "charger", "cable", "usb", "bluetooth",
        "speaker", "powerbank", "mouse", "keyboard", "led", "bulb", "battery",
        "batteries", "trimmer", "smartwatch", "adapter", "hdmi", "pendrive",
    }),
    ("Personal Care", {
        "soap", "shampoo", "conditioner", "toothpaste", "toothbrush", "facewash",
        "face", "lotion", "cream", "deodorant", "deo", "razor", "hair", "sanitizer",
        "handwash", "moisturiser", "moisturizer", "sunscreen", "talc", "lip",
    }),
    ("Household", {
        "detergent", "dishwash", "cleaner", "phenyl", "mop", "broom", "garbage",
        "freshener", "repellent", "napkins", "tissue", "scrub", "bleach",
        "disinfectant", "harpic", "lizol", "toilet", "washing",
    }),
    ("Beverages", {
        "tea", "coffee", "juice", "cola", "soda", "water", "drink", "milkshake",
        "lassi", "squash", "energy", "beverage", "coke", "pepsi", "sprite", "nectar",
    }),
    ("Home & Kitchen", {
        "pan", "tawa", "cooker", "flask", "lunchbox", "container", "knife", "plate",
        "bowl", "mug", "bedsheet", "pillow", "towel", "curtain", "kadai", "spatula",
        "glassware", "casserole",
    }),
    ("Fashion", {
        "shirt", "tshirt", "jeans", "kurta", "saree", "socks", "shoes", "sandals",
        "cap", "belt", "wallet", "jacket", "trousers", "leggings", "sneakers",
        "slippers", "dupatta",
    }),
    ("Groceries", {
        "butter", "milk", "atta", "rice", "dal", "oil", "sugar", "salt", "ghee",
        "biscuits", "biscuit", "noodles", "chips", "bread", "paneer", "curd", "masala",
        "flour", "cheese", "honey", "jam", "ketchup", "sauce", "namkeen", "chocolate",
        "oats", "cornflakes", "pasta", "maida", "besan", "poha", "turmeric", "chilli",
        "eggs", "bhujia", "pickle", "spread",
    }),
]

_BRANDS = [
    "Amul", "Tata", "Nestle", "Britannia", "Parle", "Dove", "Colgate", "Surf Excel",
    "Ariel", "Vim", "Lizol", "Harpic", "Dettol", "Lifebuoy", "Pepsodent", "Maggi",
    "Aashirvaad", "Fortune", "Saffola", "Haldiram's", "Lay's", "Kurkure", "Coca-Cola",
    "Pepsi", "Sprite", "Bisleri", "Red Bull", "Nescafe", "Bru", "Tropicana",
    "boAt", "Philips", "Syska", "Duracell", "Samsung", "Nivea", "Himalaya",
    "Pantene", "Gillette", "Milton", "Prestige", "Cello", "Borosil", "Puma", "Nike",
    "Adidas", "Levi's", "Jockey", "Bata", "Mother Dairy", "Dabur", "Patanjali",
    "MDH", "Everest", "Kissan", "Cadbury", "Sunfeast", "India Gate", "Daawat",
    "Good Knight", "Odonil", "Scotch-Brite", "Pigeon", "Wildcraft", "Portronics",
]

_UNITS = {
    "kg": "kg", "g": "g", "gm": "g", "gms": "g", "ml": "ml", "l": "L", "ltr": "L",
    "pcs": "pcs", "pc": "pc", "w": "W", "mah": "mAh", "m": "m", "cm": "cm",
}
_NUMBER_UNIT = re.compile(r"^(\d+(?:\.\d+)?)([a-z]+)$", re.I)
_PACK_OF = re.compile(r"\b(?:pack|pck|pk)\s*of\s*(\d+)\b", re.I)
_STOPWORDS = {
    "of", "the", "and", "with", "for", "in", "pack", "pck", "pk", "new", "combo",
    "a", "an", "per", "by", "to", "x", "pcs", "pc", "free", "offer", "best",
}


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def _mock_category(text):
    words = set(_words(text))
    for category, keywords in _CATEGORY_KEYWORDS:
        if words & keywords:
            return category
    return "Other"


def _mock_brand(text):
    best = None
    for brand in _BRANDS:
        match = re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(brand), text, re.I)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), brand)
    return best[1] if best else None


def _mock_title(raw_title, raw_description, brand):
    out = []
    for word in raw_title.split():
        number_unit = _NUMBER_UNIT.match(word)
        if number_unit and number_unit.group(2).lower() in _UNITS:
            out.append("%s %s" % (number_unit.group(1), _UNITS[number_unit.group(2).lower()]))
        elif word.lower() in _UNITS and out and re.match(r"^\d+(\.\d+)?$", out[-1]):
            out.append(_UNITS[word.lower()])
        elif word.isupper() or word.islower():
            out.append(word.capitalize())
        else:
            out.append(word)
    title = " ".join(out)
    if brand:
        title = re.sub(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(brand), brand, title, flags=re.I)
    pack = _PACK_OF.search(raw_title + " " + raw_description)
    if pack and not _PACK_OF.search(raw_title):
        title += " (Pack of %s)" % pack.group(1)
    return title


def _mock_tags(text, brand):
    brand_words = set(_words(brand)) if brand else set()
    tags = []
    for word in _words(text):
        if (len(word) < 3 or word in _STOPWORDS or word in brand_words
                or any(ch.isdigit() for ch in word) or word in tags):
            continue
        tags.append(word)
        if len(tags) == 5:
            break
    return tags


class MockProvider:
    """A fake LLM: fixed latency, random failures, keyword-rule answers."""

    name = "mock"

    def __init__(self, latency_ms=200, failure_rate=0.1, rng=None, sleep=time.sleep):
        self.latency_ms = latency_ms
        self.failure_rate = failure_rate
        self._rng = rng or random.Random()
        self._sleep = sleep

    def enrich(self, raw_title, raw_description):
        if self.latency_ms > 0:
            self._sleep(self.latency_ms / 1000.0)
        if self._rng.random() < self.failure_rate:
            raise LLMError("mock LLM failure")
        text = "%s %s" % (raw_title, raw_description or "")
        brand = _mock_brand(text)
        return json.dumps({
            "clean_title": _mock_title(raw_title, raw_description or "", brand),
            "category": _mock_category(text),
            "brand": brand,
            "tags": _mock_tags(text, brand),
        })


# --------------------------------------------------------------------------
# Real providers (all speak the OpenAI chat-completions format)
# --------------------------------------------------------------------------

# name -> (default base URL, API key env var or None, default model)
REAL_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.1-8b-instant"),
    "ollama": ("http://localhost:11434/v1", None, "llama3.2:3b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
               "gemini-2.0-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                   "meta-llama/llama-3.3-70b-instruct:free"),
}


class OpenAICompatibleProvider:
    """Calls any OpenAI-compatible /chat/completions endpoint using urllib."""

    def __init__(self, name, base_url, model, api_key=None, timeout_s=30.0):
        self.name = name
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self._api_key = api_key
        self.timeout_s = timeout_s

    def enrich(self, raw_title, raw_description):
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT.format(
                    title=raw_title, description=raw_description or "(none)")},
            ],
        }
        headers = {"Content-Type": "application/json", "User-Agent": "catalogiq/1.0"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retry_after = None
            try:
                retry_after = float(exc.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            detail = exc.read().decode("utf-8", "replace")[:200]
            exc.close()
            raise LLMError("%s returned HTTP %d: %s" % (self.name, exc.code, detail), retry_after)
        except (OSError, ValueError) as exc:  # network errors, timeouts, bad JSON envelope
            raise LLMError("%s request failed: %s" % (self.name, exc))
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("%s returned an unexpected response shape" % self.name)


def build_provider(cfg, env):
    """Create the provider named by LLM_PROVIDER."""
    if cfg.llm_provider == "mock":
        return MockProvider(cfg.mock_latency_ms, cfg.mock_failure_rate)
    if cfg.llm_provider not in REAL_PROVIDERS:
        raise ValueError("Unknown LLM_PROVIDER %r. Use one of: mock, %s" % (
            cfg.llm_provider, ", ".join(sorted(REAL_PROVIDERS))))
    base_url, key_env, model = REAL_PROVIDERS[cfg.llm_provider]
    api_key = None
    if key_env:
        api_key = env.get(key_env)
        if not api_key:
            raise ValueError("LLM_PROVIDER=%s needs the %s environment variable" % (
                cfg.llm_provider, key_env))
    return OpenAICompatibleProvider(
        cfg.llm_provider, cfg.llm_base_url or base_url, cfg.llm_model or model,
        api_key, cfg.llm_timeout_s)
