#!/usr/bin/env python3
"""Refresh the catalog from the providers' own published data.

    python3 tools/refresh.py [--only SOURCE] [--today YYYY-MM-DD]

Each source is a scraper reading one provider's pages or API, with no model
in the loop: what it writes can be traced to the page it read. A value that
did not change keeps its date, unless that date is older than
CONFIRM_AFTER_DAYS, in which case it is confirmed with today's date: the
daily run then proposes changes only when a provider changes something, plus
a periodic confirmation.

Scrapers never invent a value. A new model a provider starts serving gets
an offering and, when the catalog has no file for it yet, a model file with
its name and vendor only, for a contributor to source the rest.
"""

import argparse
import datetime
import html.parser
import json
import pathlib
import re
import statistics
import sys
import time
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIRM_AFTER_DAYS = 30
USER_AGENT = "xolo-catalog-refresh (+https://github.com/xolo-gateway/catalog)"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from catalog import load_yaml  # noqa: E402


# ── Files ────────────────────────────────────────────────────────────────────


class Dumper(yaml.SafeDumper):
    pass


def _str(dumper, value):
    # Long notes fold as blocks rather than as one endless line.
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


Dumper.add_representer(str, _str)


def write_yaml(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(doc, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=100)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
        print(f"updated {path.relative_to(ROOT)}")


class ParseError(ValueError):
    """A page no longer reads the way its scraper expects: the source writes
    nothing rather than a partial or misread result."""


def fetch(url, attempts=3, min_length=1000):
    """Fetches url, retrying a transient failure. A non-200 answer or a
    body shorter than min_length is an error, never a page to parse."""
    last = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise ParseError(f"{url}: status {response.status}")
                body = response.read().decode("utf-8")
            if len(body) < min_length:
                raise ParseError(f"{url}: suspiciously short answer ({len(body)} bytes)")
            return body
        except (OSError, ParseError) as error:
            last = error
            time.sleep(5 * (attempt + 1))
    raise ParseError(f"{url}: {last}")


# ── Provenance ───────────────────────────────────────────────────────────────


PROVENANCE_KEYS = ("status", "source", "date", "note")


class Stamp:
    """Dates the values a scraper writes, keeping the date of an unchanged
    value until it needs confirming."""

    def __init__(self, today, source):
        self.today = today
        self.source = source

    def merge(self, old, new, status="published", note=None):
        """Returns the block new (a dict of values) with its provenance:
        the old block's date when the values did not change. A value that
        appears or vanishes is a change too."""
        values = {k: v for k, v in new.items() if v is not None}
        block = dict(values)
        block["status"] = status
        block["source"] = self.source
        old_values = {k: v for k, v in (old or {}).items() if k not in PROVENANCE_KEYS}
        unchanged = old is not None and old_values == values and old.get("source") == self.source
        date = self.today
        if unchanged:
            try:
                age = (datetime.date.fromisoformat(self.today) - datetime.date.fromisoformat(str(old.get("date")))).days
                if age <= CONFIRM_AFTER_DAYS:
                    date = str(old["date"])
            except ValueError:
                pass
        block["date"] = date
        if note:
            block["note"] = note
        return block


# ── HTML tables ──────────────────────────────────────────────────────────────


class Tables(html.parser.HTMLParser):
    """Collects the tables of a page, each with the heading id and the plan
    tab panel (data-plan-panel) it sits under."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.heading = None
        self.panels = []
        self._depth = 0
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self._depth += 1
        if tag in ("h2", "h3") and attrs.get("id"):
            self.heading = attrs["id"]
        if "data-plan-panel" in attrs:
            self.panels.append((self._depth, attrs["data-plan-panel"]))
        if tag == "table":
            self._table = {"heading": self.heading, "panel": self.panels[-1][1] if self.panels else None, "rows": []}
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        while self.panels and self.panels[-1][0] >= self._depth:
            self.panels.pop()
        self._depth -= 1

    def handle_startendtag(self, tag, attrs):
        # A void element opens nothing to close.
        pass

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data


def page_tables(page):
    parser = Tables()
    parser.feed(page)
    return parser.tables


def as_dicts(table, expected):
    """The rows of table as dicts. Its header must hold every expected
    column and each row as many cells as the header: a table that drifts is
    an error, never a row silently dropped."""
    if not table["rows"]:
        raise ParseError("empty table")
    header, *rows = table["rows"]
    missing = [c for c in expected if c not in header]
    if missing:
        raise ParseError(f"table {header} lacks the columns {missing}")
    for row in rows:
        if len(row) != len(header):
            raise ParseError(f"row {row} does not match the header {header}")
    if not rows:
        raise ParseError(f"table {header} has no row")
    return [dict(zip(header, row)) for row in rows]


ABSENT = {"", "-", "—", "–", "n/a"}
MONEY = re.compile(r"^\$\s?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)$")


TWO_AMOUNTS = re.compile(r"^\$\s?([0-9]+(?:\.[0-9]+)?)\s+\$\s?([0-9]+(?:\.[0-9]+)?)$")


def price(text, discounted=False):
    """A price cell: '$1.40' → 1.4, 'Free' → 0, '-' → None. On a discounted
    row, '$4.00 $2.00' (the struck-out price, then the one in force) → 2.
    Any other text is an error: a misread price is worse than none."""
    t = text.strip()
    if discounted:
        m = TWO_AMOUNTS.match(t)
        if m:
            before, now = float(m.group(1)), float(m.group(2))
            if now >= before:
                raise ParseError(f"discounted price {text!r} is not lower than the struck-out one")
            return now
    if t.lower() in ABSENT:
        return None
    if re.match(r"^free", t, re.I):
        return 0.0
    m = MONEY.match(t)
    if not m:
        raise ParseError(f"unreadable price {text!r}")
    return float(m.group(1).replace(",", ""))


def limit(text):
    """A monthly limit cell: '$60' → 60; '-', 'Free' or 'Unlimited' → None,
    no limit. Any other text is an error."""
    t = text.strip()
    # "Unlimited" or "Free" may carry a note glued to it ("Unlimitedlimited
    # time"): the word is what counts.
    if t.lower() in ABSENT or re.match(r"^(free|unlimited)", t, re.I):
        return None
    m = MONEY.match(t)
    if not m:
        raise ParseError(f"unreadable limit {text!r}")
    value = float(m.group(1).replace(",", ""))
    if value <= 0:
        raise ParseError(f"non-positive limit {text!r}")
    return value


def money(text):
    """A plan price: '$10/month' → 10, None when unreadable."""
    m = re.search(r"\$\s?([0-9]+(?:\.[0-9]+)?)", text.replace(",", ""))
    return float(m.group(1)) if m else None


# ── OpenCode Go ──────────────────────────────────────────────────────────────

OPENCODE_GO_URL = "https://opencode.ai/docs/go/"

# The API an OpenCode Go endpoint path is called through, and the suffix of
# that path to strip to get the API's base URL.
OPENCODE_ENDPOINTS = [
    (re.compile(r"^(.*)/chat/completions$"), "openai"),
    (re.compile(r"^(.*)/responses$"), "openai-responses"),
    (re.compile(r"^(.*)/messages$"), "anthropic"),
    (re.compile(r"^(.*)/models/[^/:]+(?::[A-Za-z]+)?$"), "gemini"),
]

# Vendors of the model families OpenCode Go serves, by model ID prefix, for
# the stub of a model the catalog does not know yet.
VENDORS = {
    "glm": "Z.ai", "kimi": "Moonshot AI", "longcat": "Meituan", "step": "StepFun",
    "mimo": "Xiaomi", "minimax": "MiniMax", "qwen": "Alibaba Cloud", "deepseek": "DeepSeek",
    "hy": "Tencent", "grok": "xAI", "gpt": "OpenAI", "claude": "Anthropic", "gemini": "Google",
    "muse": "Muse", "beam": "Beam", "iceman": "Iceman", "zai": "Z.ai",
}


def vendor_of(model_id):
    for prefix, vendor in VENDORS.items():
        if model_id.startswith(prefix):
            return vendor
    return "Unknown"


def catalog_model_id(model_id):
    """The catalog model an offering is of: the provider's ID without the
    marketing suffixes that do not change the model."""
    return re.sub(r"-(free|contributor)$", "", model_id)


# The qualifiers a priced row may carry, the base tier first.
QUALIFIERS = [
    (re.compile(r"^$"), "base"),
    (re.compile(r"^off-peak$", re.I), "base"),
    (re.compile(r"^peak$", re.I), "peak"),
    (re.compile(r"^[≤<]=?\s*[0-9]+K tokens$", re.I), "base"),
    (re.compile(r"^>\s*([0-9]+)K tokens$", re.I), "tier"),
    # A temporary discount: the price listed is the one in force.
    (re.compile(r"^[0-9]+% off$", re.I), "discount"),
]


def qualifier_kind(qualifier):
    for pattern, kind in QUALIFIERS:
        if pattern.match(qualifier):
            return kind
    raise ParseError(f"unknown price qualifier {qualifier!r}")


def scrape_opencode_go(today, page=None):
    page = page if page is not None else fetch(OPENCODE_GO_URL)
    tables = page_tables(page)
    stamp = Stamp(today, OPENCODE_GO_URL)

    # Tables are recognised by their columns, which outlive a reorganised page.
    def columns(t):
        return t["rows"][0] if t["rows"] else []

    endpoints = next((t for t in tables if "Model ID" in columns(t) and "Endpoint" in columns(t)), None)
    if endpoints is None:
        raise ParseError("OpenCode Go: the page no longer has its endpoint table")
    by_name = {}
    for row in as_dicts(endpoints, ["Model", "Model ID", "Endpoint"]):
        model_id, endpoint = row["Model ID"], row["Endpoint"]
        for pattern, api_type in OPENCODE_ENDPOINTS:
            m = pattern.match(endpoint)
            if m:
                by_name[row["Model"]] = {"model_id": model_id, "api_type": api_type, "base_url": m.group(1)}
                break
        else:
            raise ParseError(f"OpenCode Go: unknown endpoint {endpoint!r} for {model_id}")

    usage = [t for t in tables if "Monthly limit" in columns(t) and t["panel"]]
    if not usage or not by_name:
        raise ParseError("OpenCode Go: the page no longer has its usage or endpoint tables")

    text = re.sub(r"<[^>]+>", " ", page)
    peak = re.search(r"Peak hours are ([0-9:]+-[0-9:]+)(?: and ([0-9:]+-[0-9:]+))? UTC, Monday through Friday", text)

    # Prices are the same on every plan: they are read from the first table.
    # A model listed on several rows is priced by tier ("(≤ 256K tokens)",
    # "(> 256K tokens)") or by time of day ("(Off-Peak)", "(Peak)").
    prices = {}
    limits = {}
    usage_columns = ["Model", "Input", "Output", "Cached Read", "Cached Write", "Monthly limit"]
    for table in usage:
        plan = table["panel"]
        for row in as_dicts(table, usage_columns):
            label = row["Model"]
            base = re.sub(r"\s*\(.*\)$", "", label).strip()
            qualifier = (re.search(r"\((.*)\)$", label) or [None, ""])[1].strip()
            kind = qualifier_kind(qualifier)
            if base not in by_name:
                raise ParseError(f"OpenCode Go: {label!r} is priced but not in the endpoint table")
            limits.setdefault(plan, {})[base] = limit(row["Monthly limit"])
            if table is not usage[0]:
                continue
            discounted = kind == "discount"
            if discounted:
                kind = "base"
            entry = {
                "input": price(row["Input"], discounted),
                "output": price(row["Output"], discounted),
                "cached_input": price(row["Cached Read"], discounted),
                "cache_write": price(row["Cached Write"], discounted),
            }
            if entry["input"] is None or entry["output"] is None:
                raise ParseError(f"OpenCode Go: {label!r} has no input or output price")
            if kind == "peak" and not peak:
                raise ParseError(f"OpenCode Go: {label!r} is a peak price but the page no longer states the peak hours")
            prices.setdefault(base, []).append((qualifier, kind, entry))
    # Every plan prices the same models: a plan that lists others is a
    # table read wrong.
    for plan, monthly in limits.items():
        if set(monthly) != set(limits[usage[0]["panel"]]):
            raise ParseError(f"OpenCode Go: plan {plan!r} lists other models than {usage[0]['panel']!r}")

    provider_path = ROOT / "providers" / "opencode-go.yaml"
    provider = load_yaml(provider_path) if provider_path.exists() else {}
    old_offerings = {o["model_id"]: o for o in provider.get("offerings") or []}

    offerings = []
    # The model stubs are written last, once the whole page has been read:
    # a source that fails half way writes nothing.
    stubs = []
    for name, info in by_name.items():
        rows = prices.get(name)
        if not rows:
            raise ParseError(f"OpenCode Go: {name!r} has an endpoint but no price")
        bases = [entry for _, kind, entry in rows if kind == "base"]
        if len(bases) != 1:
            raise ParseError(f"OpenCode Go: {name!r} has {len(bases)} base prices, want exactly one")
        base_row = bases[0]
        tiers, peaks = [], []
        for qualifier, kind, entry in rows:
            values = {k: v for k, v in entry.items() if v is not None}
            if kind == "peak":
                peaks.append({"hours": [h for h in peak.groups() if h], "timezone": "UTC",
                              "days": ["mon", "tue", "wed", "thu", "fri"], **values})
            elif kind == "tier":
                above = int(re.match(r"^>\s*([0-9]+)K", qualifier).group(1)) * 1000
                tiers.append({"above_input_tokens": above, **values})
        old = old_offerings.get(info["model_id"], {})
        price_values = dict(base_row)
        if tiers:
            price_values["tiers"] = tiers
        if peaks:
            price_values["peak"] = peaks
        offering = {
            "model": old.get("model") or catalog_model_id(info["model_id"]),
            "model_id": info["model_id"],
            "api_type": info["api_type"],
            "base_url": info["base_url"],
            "prices": stamp.merge(old.get("prices"), price_values),
        }
        for key in ("context_window", "output_window", "throughput"):
            if key in old:
                offering[key] = old[key]
        offerings.append(offering)
        stubs.append((offering["model"], name, vendor_of(info["model_id"])))

    plans = []
    old_plans = {p["id"]: p for p in provider.get("plans") or []}
    plan_prices = {}
    for table in tables:
        if columns(table)[:2] == ["Plan", "Price"]:
            for row in as_dicts(table, ["Plan", "Price"]):
                if "Price" in row:
                    plan_prices[row["Plan"].lower().replace(" ", "-")] = row["Price"]
    offered = {o["model_id"] for o in offerings}
    for plan_id, monthly in limits.items():
        budgets = {}
        for name, monthly_limit in monthly.items():
            info = by_name.get(name)
            if info is None or info["model_id"] not in offered:
                continue
            budgets[info["model_id"]] = monthly_limit
        # OpenCode Go meters one window per period, each model filling it at
        # its own rate: the window holds the plan's most common monthly limit,
        # and each model weighs by its own limit (a shared window).
        limited = [v for v in budgets.values() if v is not None]
        if not limited:
            raise ParseError(f"OpenCode Go: plan {plan_id!r} lists no limited model")
        reference = max(limited, key=lambda v: (limited.count(v), v))
        constraints = []
        for label, duration, share in (("5h", "5h", 0.2), ("weekly", "168h", 0.5), ("monthly", "720h", 1.0)):
            constraints.append({
                "kind": "rolling_window", "label": label, "duration": duration, "scope": "shared",
                "value_budget": round(reference * share, 2),
                "model_budgets": {m: ({} if v is None else {"value_budget": round(v * share, 2)}) for m, v in budgets.items()},
            })
        price_text = plan_prices.get(plan_id)
        amount = money(price_text) if price_text else None
        plan = {"id": plan_id, "name": plan_id.replace("-", " ").title().replace("Go ", "Go "), "constraints": constraints}
        if amount is not None:
            plan["price"] = {"amount": amount, "period": "month"}
        old = old_plans.get(plan_id)
        stamped = stamp.merge(old and {k: old.get(k) for k in ("constraints", "price") if k in old} | {"date": old.get("date"), "source": old.get("source")},
                              {k: plan[k] for k in ("constraints", "price") if k in plan},
                              note="Each window is shared by every model, each weighing by its own budget: 20%, 50% and 100% of its published monthly limit "
                                   "for the 5-hour, weekly and monthly windows. The window's own budget is the plan's most common monthly limit, at the same share. "
                                   "The page does not say whether the windows slide or reset on a schedule, and \"monthly\" is approximated as 720 hours. "
                                   "A model listed with no budget is free for a limited time.")
        plans.append({"id": plan_id, "name": (old or {}).get("name") or plan["name"], **{k: v for k, v in stamped.items()}})

    for stub in stubs:
        ensure_model(*stub)
    write_yaml(provider_path, {
        "id": "opencode-go",
        "name": "OpenCode Go",
        "website": "https://opencode.ai/go",
        "currency": "USD",
        # The /v1 root serves the model list as well as chat completions.
        "xolo": {"type": "openai", "base_url": "https://opencode.ai/inference/go/v1"},
        "plans": plans,
        "offerings": sorted(offerings, key=lambda o: o["model_id"]),
    })


# ── OpenRouter ───────────────────────────────────────────────────────────────

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{}/endpoints"
OPENROUTER_PRICES_NOTE = (
    "Median of the prices of the providers OpenRouter serves this model through at "
    "the standard tier (every tier when it has none), those without a current incident "
    "(all of them when every one has one); a cache price only when at least half of them state one. A request is "
    "billed at the price of the provider it reaches, cheaper or dearer: the price the "
    "model list shows is one provider's, and moves whenever a provider comes, goes or "
    "runs a discount."
)


def openrouter_median_prices(model_id, document):
    """The median of each price across the providers of model_id that have
    no current incident, per million tokens. A field no provider prices is
    None."""
    try:
        endpoints = json.loads(document)["data"]["endpoints"]
    except (ValueError, KeyError, TypeError) as error:
        raise ParseError(f"OpenRouter: unreadable providers of {model_id}: {error}") from error
    if not isinstance(endpoints, list) or not endpoints:
        raise ParseError(f"OpenRouter: no provider serves {model_id}")
    # A flex or priority tier is reached only by a request asking for it.
    endpoints = [e for e in endpoints if isinstance(e, dict)]
    endpoints = [e for e in endpoints if e.get("service_tier") in (None, "", "standard")] or endpoints
    # Without a status, the incident filter would turn itself off, or keep
    # only the few providers still stating one, unseen.
    if not all(type(e.get("status")) is int for e in endpoints):
        raise ParseError(f"OpenRouter: a provider of {model_id} states no status")
    healthy = [e for e in endpoints if isinstance(e, dict) and e.get("status") == 0]
    # Every provider in trouble at once still has prices worth reading.
    endpoints = healthy or [e for e in endpoints if isinstance(e, dict)]

    def median(key, required=False):
        """A cache price stated by a few providers only would be theirs, not
        the market's: it needs at least half of them."""
        amounts = []
        for e in endpoints:
            value = (e.get("pricing") or {}).get(key)
            if value in (None, ""):
                continue
            try:
                amount = float(value)
            except (TypeError, ValueError):
                raise ParseError(f"OpenRouter: {model_id} has an unreadable {key} price {value!r}")
            # A negative price is OpenRouter's "varies": no figure to keep.
            if amount >= 0:
                amounts.append(amount)
        if not amounts:
            if required:
                raise ParseError(f"OpenRouter: {model_id} has no fixed {key} price")
            return None
        if not required and 2 * len(amounts) < len(endpoints):
            return None
        return round(statistics.median(amounts) * 1_000_000, 6)

    return {
        "input": median("prompt", required=True),
        "cached_input": median("input_cache_read"),
        "cache_write": median("input_cache_write"),
        "output": median("completion", required=True),
    }


def scrape_openrouter(today, document=None, endpoints=None):
    """Refreshes the offerings already in providers/openrouter.yaml: OpenRouter
    serves hundreds of models, the catalog keeps those someone added.

    OpenRouter is a market: a model is served by many providers, at prices
    from one to ten times apart. Their median is the offering's price; the
    model list gives the windows. endpoints(model_id) returns a model's
    providers as OpenRouter's API does, for the tests."""
    path = ROOT / "providers" / "openrouter.yaml"
    if not path.exists():
        return
    provider = load_yaml(path)
    try:
        data = json.loads(document if document is not None else fetch(OPENROUTER_MODELS_URL))["data"]
    except (ValueError, KeyError, TypeError) as error:
        raise ParseError(f"OpenRouter: unreadable model list: {error}") from error
    if not isinstance(data, list) or len(data) < 50:
        raise ParseError(f"OpenRouter: a model list of {len(data) if isinstance(data, list) else '?'} entries is not the full list")
    listed = {m["id"]: m for m in data if isinstance(m, dict) and "id" in m}
    if endpoints is None:
        def endpoints(model_id):
            time.sleep(0.5)
            # One provider's entry is about 1.5 kB: the floor of a page does not apply.
            return fetch(OPENROUTER_ENDPOINTS_URL.format(model_id), min_length=100)
    stamp = Stamp(today, OPENROUTER_MODELS_URL)

    offerings = []
    for old in provider.get("offerings") or []:
        m = listed.get(old["model_id"])
        if m is None:
            print(f"warning: OpenRouter no longer lists {old['model_id']}", file=sys.stderr)
            offerings.append(old)
            continue
        offering = dict(old)
        prices = Stamp(today, OPENROUTER_ENDPOINTS_URL.format(old["model_id"]))
        offering["prices"] = prices.merge(old.get("prices"), openrouter_median_prices(old["model_id"], endpoints(old["model_id"])),
                                          status="estimated", note=OPENROUTER_PRICES_NOTE)
        top = m.get("top_provider") or {}
        if m.get("context_length"):
            offering["context_window"] = stamp.merge(old.get("context_window"), {"value": int(m["context_length"])})
        if top.get("max_completion_tokens"):
            offering["output_window"] = stamp.merge(old.get("output_window"), {"value": int(top["max_completion_tokens"])})
        offerings.append(offering)
    provider["offerings"] = offerings
    write_yaml(path, provider)


# ── Mistral ──────────────────────────────────────────────────────────────────

MISTRAL_DOCS = "https://docs.mistral.ai"
MISTRAL_INDEX_URL = MISTRAL_DOCS + "/models/model-cards/mistral-medium-3-5-26-04"
MISTRAL_CARD_URL = MISTRAL_DOCS + "/models/model-cards/{}"
MISTRAL_SOURCE = MISTRAL_DOCS + "/models"
# Fewer priced models than this means a page read wrong, not a catalog that
# shrank overnight.
MISTRAL_MIN_PRICED = 10
# The third-party models Mistral hosts, by its API name, and the catalog
# model each one is: their figures are already sourced there.
MISTRAL_HOSTED = {
    "zai-glm-5-3": "glm-5.3",
    "zai-glm-5-3-flash": "glm-5.3-flash",
    "deepseek-v4-1-flash-1017": "deepseek-v4.1-flash",
}


def next_payload(page):
    """The data a Next.js page streams to the browser (self.__next_f), as
    one text: the docs' cards carry their figures there, as JSON."""
    decoder = json.JSONDecoder()
    chunks = []
    marker = "self.__next_f.push("
    at = page.find(marker)
    while at >= 0:
        try:
            pushed, _ = decoder.raw_decode(page, at + len(marker))
        except ValueError as error:
            raise ParseError(f"unreadable Next.js payload: {error}") from error
        # [1, "text"] carries the payload; other pushes set up the stream.
        if isinstance(pushed, list) and len(pushed) == 2 and pushed[0] == 1 and isinstance(pushed[1], str):
            chunks.append(pushed[1])
        at = page.find(marker, at + len(marker))
    if not chunks:
        raise ParseError("the page streams no Next.js payload")
    return "".join(chunks)


def json_after(payload, marker, opening):
    """Decodes the JSON value that follows marker, which must start with
    opening ('[' or '{')."""
    i = payload.find(marker)
    if i < 0:
        raise ParseError(f"no {marker!r} in the page")
    rest = payload[i + len(marker):]
    if not rest.startswith(opening):
        raise ParseError(f"{marker!r} is not followed by {opening!r}")
    try:
        value, _ = json.JSONDecoder().raw_decode(rest)
    except ValueError as error:
        raise ParseError(f"unreadable JSON after {marker!r}: {error}") from error
    return value


def mistral_cards(index_page):
    """The slugs and titles of the model cards the docs' navigation lists."""
    cards = json_after(next_payload(index_page), '"metadata":{"label":"Model Cards"},"children":', "[")
    listed = []
    for card in cards:
        slug = (card.get("slug") or [None])[-1]
        title = (card.get("metadata") or {}).get("title")
        if not slug or not title or not re.match(r"^[a-z0-9][a-z0-9.-]*$", slug):
            raise ParseError(f"Mistral: unreadable model card entry {card!r}")
        listed.append((slug, title))
    if len(listed) < 20:
        raise ParseError(f"Mistral: {len(listed)} model cards is not the full list")
    return listed


TOKEN_COUNT = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([kKmM])$")


def context_tokens(text):
    """A card's context size: '256k' → 262144, '1M' → 1048576."""
    m = TOKEN_COUNT.match(text.strip())
    if not m:
        raise ParseError(f"unreadable context size {text!r}")
    unit = 1024 if m.group(2).lower() == "k" else 1024 * 1024
    return int(float(m.group(1)) * unit)


def mistral_card(page, slug):
    """What one card states: the model's API names, whether it is retired,
    its token prices and its context size. None for a card Xolo cannot
    import as a priced model (retired, or not priced in input and output
    tokens), which is left out."""
    payload = next_payload(page)
    # A retired or deprecated model's card carries a badge saying so: no
    # longer one to import, whatever else the card states. The badge names
    # its type, or, on the older cards, says it in its text.
    if re.search(r'"data-badge-type":"(retired|deprecated)"|"children":"(Retired|Deprecated)"', payload):
        return None
    names = json_after(payload, '{"names":', "[")
    # A card without an API name describes a model the API does not serve.
    if names == []:
        return None
    if not all(isinstance(n, str) and re.match(r"^[a-z0-9][a-z0-9.-]*$", n) for n in names):
        raise ParseError(f"Mistral {slug}: unreadable model names {names!r}")
    # The price block, wherever it sits among its component's props; a
    # second copy refers to it as "$…" and is skipped.
    i = payload.find('"pricing":{')
    if i < 0:
        raise ParseError(f"Mistral {slug}: the card states no price")
    card = json_after(payload[i:], '"pricing":', "{")
    retired = re.search(r'"isRetired":(true|false)', payload[i:])
    if not retired:
        raise ParseError(f"Mistral {slug}: the card does not say whether the model is retired")
    if retired.group(1) == "true":
        return None

    # The API a model is called through decides what Xolo can import: chat
    # completions, or embeddings priced on input only.
    if "/v1/chat/completions" in payload:
        kind = "completion"
    elif "/v1/embeddings" in payload:
        kind = "embeddings"
    else:
        return None

    def token_prices(side):
        """The token prices of one side, by label: an unlabelled single
        entry is the side's own price. None when the side is priced in
        anything but tokens, or under a label Xolo does not know."""
        prices = {}
        for entry in card.get(side) or []:
            if entry.get("denominator") != "/M Tokens":
                return None
            value = entry.get("price")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ParseError(f"Mistral {slug}: unreadable {side} price {entry!r}")
            label = (entry.get("label") or side).strip().lower()
            if label not in (side, "cached input") or label in prices:
                return None
            prices[label] = float(value)
        return prices

    if card.get("free") is True:
        prices = {"input": 0.0, "output": 0.0}
    else:
        inputs, outputs = token_prices("input"), token_prices("output")
        if inputs is None or outputs is None or "input" not in inputs:
            return None
        if kind == "completion" and "output" not in outputs:
            return None
        prices = {"input": inputs["input"], "output": outputs.get("output", 0.0)}
        if "cached input" in inputs:
            prices["cached_input"] = inputs["cached input"]

    # Not every card states a context size: an absent one is left unknown,
    # one that is there must read.
    context = None
    if '"contextSize"' in payload:
        m = re.search(r'"contextSize",\{[^{}]*"children":\["\$","span",null,\{[^{}]*"children":"([^"]+)"', payload)
        if not m:
            raise ParseError(f"Mistral {slug}: unreadable context size")
        context = context_tokens(m.group(1))
    return {"names": names, "prices": prices, "context": context, "kind": kind}


def scrape_mistral(today, pages=None, index=None):
    """Refreshes the models Mistral's docs list as priced in tokens. pages
    and index, for the tests, give the cards by URL and the page holding
    the navigation."""
    get = (lambda url: pages[url]) if pages is not None else fetch
    stamp = Stamp(today, MISTRAL_SOURCE)
    path = ROOT / "providers" / "mistral.yaml"
    old_offerings = {}
    if path.exists():
        old_offerings = {o["model_id"]: o for o in load_yaml(path).get("offerings") or []}

    offerings, stubs, seen = [], [], set()
    for slug, title in mistral_cards(index if index is not None else get(MISTRAL_INDEX_URL)):
        if pages is None:
            time.sleep(0.5)
        card = mistral_card(get(MISTRAL_CARD_URL.format(slug)), slug)
        if card is None:
            continue
        model_id = card["names"][0]
        if model_id in seen:
            raise ParseError(f"Mistral: {model_id} is listed by two cards")
        seen.add(model_id)
        old = old_offerings.get(model_id, {})
        offering = {
            "model": old.get("model") or MISTRAL_HOSTED.get(model_id, model_id),
            "model_id": model_id,
            "prices": stamp.merge(old.get("prices"), card["prices"]),
        }
        if card["context"] is not None:
            offering["context_window"] = stamp.merge(old.get("context_window"), {"value": card["context"]},
                                                     note="The card states the size in k tokens, read as multiples of 1024.")
        offerings.append(offering)
        stubs.append((offering["model"], title, vendor_of(model_id) if vendor_of(model_id) != "Unknown" else "Mistral AI", card["kind"]))
    if len(offerings) < MISTRAL_MIN_PRICED:
        raise ParseError(f"Mistral: {len(offerings)} priced models is not the full list")

    for stub in stubs:
        ensure_model(*stub)
    write_yaml(path, {
        "id": "mistral",
        "name": "Mistral AI",
        "website": "https://mistral.ai",
        "currency": "USD",
        "xolo": {"type": "mistral", "base_url": "https://api.mistral.ai/v1"},
        "offerings": sorted(offerings, key=lambda o: o["model_id"]),
    })


# ── Models ───────────────────────────────────────────────────────────────────


def ensure_model(model_id, name, vendor, kind="completion"):
    """Creates the stub of a model the catalog does not know yet."""
    path = ROOT / "models" / f"{model_id}.yaml"
    if path.exists():
        return
    stub = {"id": model_id, "name": name, "vendor": vendor}
    if kind != "completion":
        stub["kind"] = kind
    write_yaml(path, stub)


SOURCES = {
    "opencode-go": scrape_opencode_go,
    "mistral": scrape_mistral,
    "openrouter": scrape_openrouter,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=sorted(SOURCES))
    parser.add_argument("--today", default=datetime.date.today().isoformat())
    args = parser.parse_args()
    failed = []
    for name, scrape in SOURCES.items():
        if args.only and name != args.only:
            continue
        try:
            scrape(args.today)
        except Exception as error:  # one broken source must not stop the others
            print(f"::error::{name}: {error}")
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
