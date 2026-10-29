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
import sys
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


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


# ── Provenance ───────────────────────────────────────────────────────────────


class Stamp:
    """Dates the values a scraper writes, keeping the date of an unchanged
    value until it needs confirming."""

    def __init__(self, today, source):
        self.today = today
        self.source = source

    def merge(self, old, new, status="published", note=None):
        """Returns the block new (a dict of values) with its provenance:
        the old block's date when the values did not change."""
        values = {k: v for k, v in new.items() if v is not None}
        block = dict(values)
        block["status"] = status
        block["source"] = self.source
        unchanged = old is not None and all(old.get(k) == v for k, v in values.items()) and old.get("source") == self.source
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


def as_dicts(table):
    header, *rows = table["rows"]
    return [dict(zip(header, row)) for row in rows if len(row) == len(header)]


def money(text):
    """'$1.40' → 1.4; 'Free' → 0; '-' or '' → None."""
    if text.strip().lower().startswith("free"):
        return 0.0
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text.replace(",", ""))
    return float(m.group(1)) if m and "$" in text else None


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
    "muse": "Muse", "beam": "Beam", "iceman": "Iceman",
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


def scrape_opencode_go(today):
    page = fetch(OPENCODE_GO_URL)
    tables = page_tables(page)
    stamp = Stamp(today, OPENCODE_GO_URL)

    # Tables are recognised by their columns, which outlive a reorganised page.
    def columns(t):
        return t["rows"][0] if t["rows"] else []

    endpoints = next((t for t in tables if "Model ID" in columns(t) and "Endpoint" in columns(t)), None)
    if endpoints is None:
        raise ValueError("OpenCode Go: the page no longer has its endpoint table")
    by_name = {}
    for row in as_dicts(endpoints):
        model_id, endpoint = row["Model ID"], row["Endpoint"]
        for pattern, api_type in OPENCODE_ENDPOINTS:
            m = pattern.match(endpoint)
            if m:
                by_name[row["Model"]] = {"model_id": model_id, "api_type": api_type, "base_url": m.group(1)}
                break
        else:
            raise ValueError(f"OpenCode Go: unknown endpoint {endpoint!r} for {model_id}")

    usage = [t for t in tables if "Monthly limit" in columns(t) and t["panel"]]
    if not usage or not by_name:
        raise ValueError("OpenCode Go: the page no longer has its usage or endpoint tables")

    text = re.sub(r"<[^>]+>", " ", page)
    peak = re.search(r"Peak hours are ([0-9:]+-[0-9:]+)(?: and ([0-9:]+-[0-9:]+))? UTC, Monday through Friday", text)

    # Prices are the same on every plan: they are read from the first table.
    # A model listed on several rows is priced by tier ("(≤ 256K tokens)",
    # "(> 256K tokens)") or by time of day ("(Off-Peak)", "(Peak)").
    prices = {}
    limits = {}
    for table in usage:
        plan = table["panel"]
        for row in as_dicts(table):
            label = row["Model"]
            base = re.sub(r"\s*\(.*\)$", "", label).strip()
            qualifier = (re.search(r"\((.*)\)$", label) or [None, ""])[1]
            limits.setdefault(plan, {})[base] = money(row.get("Monthly limit", ""))
            if table is not usage[0]:
                continue
            entry = {
                "input": money(row["Input"]),
                "output": money(row["Output"]),
                "cached_input": money(row.get("Cached Read", "")),
                "cache_write": money(row.get("Cached Write", "")),
            }
            prices.setdefault(base, []).append((qualifier, entry))

    provider_path = ROOT / "providers" / "opencode-go.yaml"
    provider = load_yaml(provider_path) if provider_path.exists() else {}
    old_offerings = {o["model_id"]: o for o in provider.get("offerings") or []}

    offerings = []
    for name, info in by_name.items():
        rows = prices.get(name)
        if not rows:
            print(f"warning: OpenCode Go lists {name} with no price", file=sys.stderr)
            continue
        base_row = rows[0][1]
        tiers, peaks = [], []
        for qualifier, entry in rows:
            if qualifier.lower() == "peak" and peak:
                peaks.append({"hours": [h for h in peak.groups() if h], "timezone": "UTC",
                              "days": ["mon", "tue", "wed", "thu", "fri"],
                              **{k: v for k, v in entry.items() if v is not None}})
            elif qualifier.lower() == "off-peak":
                base_row = entry
            elif m := re.match(r">\s*([0-9]+)K", qualifier):
                tiers.append({"above_input_tokens": int(m.group(1)) * 1000, **{k: v for k, v in entry.items() if v is not None}})
            elif re.match(r"[≤<]", qualifier):
                base_row = entry
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
        ensure_model(offering["model"], name, vendor_of(info["model_id"]))

    plans = []
    old_plans = {p["id"]: p for p in provider.get("plans") or []}
    plan_prices = {}
    for table in tables:
        if columns(table)[:2] == ["Plan", "Price"]:
            for row in as_dicts(table):
                if "Price" in row:
                    plan_prices[row["Plan"].lower().replace(" ", "-")] = row["Price"]
    offered = {o["model_id"] for o in offerings}
    for plan_id, monthly in limits.items():
        budgets = {}
        for name, limit in monthly.items():
            info = by_name.get(name)
            if info is None or info["model_id"] not in offered:
                continue
            budgets[info["model_id"]] = limit
        constraints = []
        for label, duration, share in (("5h", "5h", 0.2), ("weekly", "168h", 0.5), ("monthly", "720h", 1.0)):
            constraints.append({
                "kind": "rolling_window", "label": label, "duration": duration, "scope": "model",
                "model_budgets": {m: ({} if v is None else {"value_budget": round(v * share, 2)}) for m, v in budgets.items()},
            })
        price = plan_prices.get(plan_id)
        amount = money(price) if price else None
        plan = {"id": plan_id, "name": plan_id.replace("-", " ").title().replace("Go ", "Go "), "constraints": constraints}
        if amount is not None:
            plan["price"] = {"amount": amount, "period": "month"}
        old = old_plans.get(plan_id)
        stamped = stamp.merge(old and {k: old.get(k) for k in ("constraints", "price") if k in old} | {"date": old.get("date"), "source": old.get("source")},
                              {k: plan[k] for k in ("constraints", "price") if k in plan},
                              note="The 5-hour, weekly and monthly budgets are 20%, 50% and 100% of each model's published monthly limit. "
                                   "The page does not say whether the windows slide or reset on a schedule, and \"monthly\" is approximated as 720 hours. "
                                   "A model listed with no budget is free for a limited time.")
        plans.append({"id": plan_id, "name": (old or {}).get("name") or plan["name"], **{k: v for k, v in stamped.items()}})

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


def scrape_openrouter(today):
    """Refreshes the offerings already in providers/openrouter.yaml: OpenRouter
    serves hundreds of models, the catalog keeps those someone added."""
    path = ROOT / "providers" / "openrouter.yaml"
    if not path.exists():
        return
    provider = load_yaml(path)
    listed = {m["id"]: m for m in json.loads(fetch(OPENROUTER_MODELS_URL))["data"]}
    stamp = Stamp(today, OPENROUTER_MODELS_URL)

    offerings = []
    for old in provider.get("offerings") or []:
        m = listed.get(old["model_id"])
        if m is None:
            print(f"warning: OpenRouter no longer lists {old['model_id']}", file=sys.stderr)
            offerings.append(old)
            continue
        pricing = m.get("pricing") or {}

        def per_million(key):
            value = pricing.get(key)
            return round(float(value) * 1_000_000, 6) if value not in (None, "", "-1") else None

        offering = dict(old)
        offering["prices"] = stamp.merge(old.get("prices"), {
            "input": per_million("prompt"),
            "cached_input": per_million("input_cache_read"),
            "cache_write": per_million("input_cache_write"),
            "output": per_million("completion"),
        })
        top = m.get("top_provider") or {}
        if m.get("context_length"):
            offering["context_window"] = stamp.merge(old.get("context_window"), {"value": int(m["context_length"])})
        if top.get("max_completion_tokens"):
            offering["output_window"] = stamp.merge(old.get("output_window"), {"value": int(top["max_completion_tokens"])})
        offerings.append(offering)
    provider["offerings"] = offerings
    write_yaml(path, provider)


# ── Models ───────────────────────────────────────────────────────────────────


def ensure_model(model_id, name, vendor):
    """Creates the stub of a model the catalog does not know yet."""
    path = ROOT / "models" / f"{model_id}.yaml"
    if path.exists():
        return
    write_yaml(path, {"id": model_id, "name": name, "vendor": vendor})


SOURCES = {
    "opencode-go": scrape_opencode_go,
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
