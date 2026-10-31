"""Tests of the scrapers and the merge guard, on frozen copies of the pages
they read (tools/fixtures). Run with: python3 -m unittest discover -s tools
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import catalog  # noqa: E402
import guard  # noqa: E402
import refresh  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
REPO = pathlib.Path(__file__).resolve().parent.parent


class CatalogCopy(unittest.TestCase):
    """Runs each test on a copy of the catalog, never on the repository."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        for directory in ("providers", "models", "schema"):
            shutil.copytree(REPO / directory, self.root / directory)
        self.saved = refresh.ROOT, catalog.ROOT
        refresh.ROOT = catalog.ROOT = self.root

    def tearDown(self):
        refresh.ROOT, catalog.ROOT = self.saved
        shutil.rmtree(self.root)

    def provider(self, name):
        return catalog.load_yaml(self.root / "providers" / f"{name}.yaml")


def opencode_page():
    return (FIXTURES / "opencode-go.html").read_text(encoding="utf-8")


class OpenCodeGoTest(CatalogCopy):
    def scrape(self, page=None):
        refresh.scrape_opencode_go("2026-10-29", page=page if page is not None else opencode_page())
        return self.provider("opencode-go")

    def test_reads_every_offering_and_plan(self):
        p = self.scrape()
        offerings = {o["model_id"]: o for o in p["offerings"]}
        self.assertEqual(len(offerings), 30)
        self.assertEqual([plan["id"] for plan in p["plans"]], ["go", "go-plus", "go-teams"])
        # One window per period, shared by every model, each weighing by its
        # own limit; the window holds the plan's most common limit.
        five_hours = p["plans"][0]["constraints"][0]
        self.assertEqual((five_hours["scope"], five_hours["label"]), ("shared", "5h"))
        limits = [b["value_budget"] for b in five_hours["model_budgets"].values() if b]
        self.assertEqual(five_hours["value_budget"], max(limits, key=lambda v: (limits.count(v), v)))

        deepseek = offerings["deepseek-v4.1-flash"]["prices"]
        self.assertEqual((deepseek["input"], deepseek["output"]), (0.15, 0.6))
        self.assertEqual(deepseek["peak"][0]["hours"], ["01:00-04:00", "06:00-10:00"])

        qwen = offerings["qwen3.7-plus"]["prices"]
        self.assertEqual(qwen["tiers"][0]["above_input_tokens"], 256_000)

        # The discounted row reads the price in force, not the struck-out one.
        argon = offerings["gemini-4-argon"]["prices"]
        self.assertEqual((argon["input"], argon["output"]), (2.0, 10.0))
        self.assertEqual(offerings["gemini-4-argon"]["api_type"], "gemini")

    def test_is_deterministic(self):
        first = self.scrape()
        self.assertEqual(self.scrape(), first)

    def test_refuses_an_unreadable_price(self):
        page = opencode_page().replace("$1.40", "1.40 USD", 1)
        with self.assertRaises(refresh.ParseError):
            self.scrape(page)

    def test_refuses_a_missing_column(self):
        page = opencode_page().replace("Cached Write", "Cache Writes")
        with self.assertRaises(refresh.ParseError):
            self.scrape(page)

    def test_refuses_an_unknown_qualifier(self):
        page = opencode_page().replace("(Off-Peak)", "(Night)", 1)
        with self.assertRaises(refresh.ParseError):
            self.scrape(page)

    def test_refuses_an_unknown_endpoint(self):
        page = opencode_page().replace("/chat/completions", "/v2/generate", 1)
        with self.assertRaises(refresh.ParseError):
            self.scrape(page)

    def test_refuses_peak_prices_without_peak_hours(self):
        page = opencode_page().replace("Peak hours are", "Busy hours are")
        with self.assertRaises(refresh.ParseError):
            self.scrape(page)

    def test_a_failing_page_writes_nothing(self):
        before = sorted(p.name for p in (self.root / "models").iterdir())
        page = opencode_page().replace("$1.40", "1.40 USD", 1)
        provider = (self.root / "providers" / "opencode-go.yaml").read_text()
        with self.assertRaises(refresh.ParseError):
            self.scrape(page)
        self.assertEqual(sorted(p.name for p in (self.root / "models").iterdir()), before)
        self.assertEqual((self.root / "providers" / "opencode-go.yaml").read_text(), provider)


def openrouter_endpoints(*prices, status=0):
    """A model's providers as OpenRouter's API lists them, from (input,
    output) prices per million tokens."""
    return json.dumps({"data": {"endpoints": [
        {"status": status, "pricing": {"prompt": str(i / 1e6), "completion": str(o / 1e6)}} for i, o in prices
    ]}})


class OpenRouterTest(CatalogCopy):
    def document(self):
        return json.loads((FIXTURES / "openrouter-models.json").read_text(encoding="utf-8"))

    def scrape(self, document=None, endpoints=None):
        refresh.scrape_openrouter(
            "2026-10-29",
            document=json.dumps(document or self.document()),
            endpoints=endpoints or (lambda model_id: openrouter_endpoints((0.1, 0.5), (1.4, 4.4), (1.2, 4.0))),
        )

    def test_refreshes_the_listed_offerings_at_the_median_price(self):
        self.scrape()
        offers = self.provider("openrouter")["offerings"]
        self.assertGreater(len(offers), 20)
        for o in offers:
            self.assertEqual((o["prices"]["input"], o["prices"]["output"]), (1.2, 4.0))
            self.assertEqual(o["prices"]["status"], "estimated")
            self.assertIn("Median", o["prices"]["note"])
            self.assertNotIn("cached_input", o["prices"])

    def test_the_median_leaves_out_the_providers_in_trouble(self):
        doc = json.loads(openrouter_endpoints((0.1, 0.5), (1.4, 4.4), (1.2, 4.0)))
        doc["data"]["endpoints"] += json.loads(openrouter_endpoints((9, 9), (9, 9), (9, 9), status=-2))["data"]["endpoints"]
        prices = refresh.openrouter_median_prices("m", json.dumps(doc))
        self.assertEqual((prices["input"], prices["output"]), (1.2, 4.0))

    def test_the_median_of_an_even_count_is_the_mean_of_the_middle_two(self):
        prices = refresh.openrouter_median_prices("m", openrouter_endpoints((0.1, 0.5), (0.3, 1.5)))
        self.assertEqual((prices["input"], prices["output"]), (0.2, 1.0))

    def test_every_provider_in_trouble_still_gives_a_price(self):
        prices = refresh.openrouter_median_prices("m", openrouter_endpoints((0.1, 0.5), (0.3, 1.5), (0.2, 1.0), status=-2))
        self.assertEqual((prices["input"], prices["output"]), (0.2, 1.0))

    def test_a_cache_price_needs_half_of_the_providers(self):
        doc = json.loads(openrouter_endpoints((1, 4), (1, 4), (1, 4), (1, 4)))
        for i, e in enumerate(doc["data"]["endpoints"]):
            e["pricing"]["input_cache_read"] = str((0.1 + i * 0.1) / 1e6)
        doc["data"]["endpoints"][0]["pricing"]["input_cache_write"] = str(2 / 1e6)
        prices = refresh.openrouter_median_prices("m", json.dumps(doc))
        self.assertEqual(prices["cached_input"], 0.25)
        self.assertIsNone(prices["cache_write"])
        doc["data"]["endpoints"][1]["pricing"]["input_cache_write"] = str(3 / 1e6)
        self.assertEqual(refresh.openrouter_median_prices("m", json.dumps(doc))["cache_write"], 2.5)

    def test_a_failing_provider_list_writes_nothing(self):
        before = (self.root / "providers" / "openrouter.yaml").read_text()

        def failing(model_id):
            raise refresh.ParseError("status 429")
        with self.assertRaises(refresh.ParseError):
            self.scrape(endpoints=failing)
        self.assertEqual((self.root / "providers" / "openrouter.yaml").read_text(), before)

    def test_refuses_an_unreadable_price(self):
        with self.assertRaises(refresh.ParseError):
            refresh.openrouter_median_prices("m", json.dumps({"data": {"endpoints": [
                {"status": 0, "pricing": {"prompt": "about a dollar", "completion": "0.000001"}},
            ]}}))

    def test_refuses_a_model_no_provider_serves(self):
        with self.assertRaises(refresh.ParseError):
            self.scrape(endpoints=lambda model_id: json.dumps({"data": {"endpoints": []}}))

    def test_publishes_the_cache_prices_most_providers_state(self):
        doc = json.loads(openrouter_endpoints((1, 4), (1, 4), (1, 4), (1, 4)))
        for i, e in enumerate(doc["data"]["endpoints"]):
            e["pricing"]["input_cache_read"] = str(0.1 / 1e6)
            if i % 2:
                e["pricing"]["input_cache_write"] = str(2 / 1e6)
        self.scrape(endpoints=lambda model_id: json.dumps(doc))
        for o in self.provider("openrouter")["offerings"]:
            self.assertEqual((o["prices"]["cached_input"], o["prices"]["cache_write"]), (0.1, 2.0))
            self.assertEqual(o["prices"]["status"], "estimated")
            self.assertIn("Median", o["prices"]["note"])

    def test_keeps_an_offering_the_list_no_longer_has(self):
        gone = self.provider("openrouter")["offerings"][0]
        doc = self.document()
        doc["data"] = [m for m in doc["data"] if m["id"] != gone["model_id"]]
        self.scrape(document=doc)
        kept = {o["model_id"]: o for o in self.provider("openrouter")["offerings"]}
        self.assertEqual(kept[gone["model_id"]], gone)

    def test_the_median_leaves_out_the_flex_and_priority_tiers(self):
        doc = json.loads(openrouter_endpoints((1, 5), (2, 10), (2.2, 11), (3.6, 18), (3.6, 18)))
        for e, tier in zip(doc["data"]["endpoints"], ("flex", None, "standard", "priority", "priority")):
            e["service_tier"] = tier
        prices = refresh.openrouter_median_prices("m", json.dumps(doc))
        self.assertEqual((prices["input"], prices["output"]), (2.1, 10.5))

    def test_a_model_with_no_standard_tier_is_priced_at_every_tier(self):
        doc = json.loads(openrouter_endpoints((1, 5), (3, 15)))
        for e, tier in zip(doc["data"]["endpoints"], ("flex", "priority")):
            e["service_tier"] = tier
        prices = refresh.openrouter_median_prices("m", json.dumps(doc))
        self.assertEqual((prices["input"], prices["output"]), (2.0, 10.0))

    def test_refuses_providers_without_a_status(self):
        doc = json.loads(openrouter_endpoints((1, 4), (2, 5)))
        for e in doc["data"]["endpoints"]:
            del e["status"]
        with self.assertRaises(refresh.ParseError):
            refresh.openrouter_median_prices("m", json.dumps(doc))

    def test_refuses_a_provider_without_a_status_among_others(self):
        doc = json.loads(openrouter_endpoints((1, 4), (2, 5), (3, 6)))
        doc["data"]["endpoints"][1]["status"] = "0"
        with self.assertRaises(refresh.ParseError):
            refresh.openrouter_median_prices("m", json.dumps(doc))

    def test_refuses_a_truncated_list(self):
        doc = self.document()
        doc["data"] = doc["data"][:10]
        with self.assertRaises(refresh.ParseError):
            self.scrape(document=doc)


class StampTest(unittest.TestCase):
    def test_keeps_the_date_of_unchanged_values(self):
        old = {"input": 1.0, "status": "published", "source": "https://a", "date": "2026-10-20"}
        self.assertEqual(refresh.Stamp("2026-10-29", "https://a").merge(old, {"input": 1.0})["date"], "2026-10-20")

    def test_a_vanishing_value_is_a_change(self):
        old = {"input": 1.0, "cache_write": 2.0, "status": "published", "source": "https://a", "date": "2026-10-20"}
        block = refresh.Stamp("2026-10-29", "https://a").merge(old, {"input": 1.0, "cache_write": None})
        self.assertEqual(block["date"], "2026-10-29")
        self.assertNotIn("cache_write", block)


class GuardTest(unittest.TestCase):
    def prices(self, **values):
        return {"input": 1.0, "output": 2.0, **values}

    def check(self, before, after):
        reasons = []
        guard.compare_prices("x", before, after, reasons)
        return reasons

    def test_small_price_moves_are_safe(self):
        self.assertEqual(self.check(self.prices(), self.prices(input=1.1)), [])

    def test_large_price_moves_need_a_review(self):
        self.assertTrue(self.check(self.prices(), self.prices(input=2.0)))
        self.assertTrue(self.check(self.prices(), self.prices(input=0.0)))
        self.assertTrue(self.check(self.prices(), {"output": 2.0}))

    def test_a_vanishing_offering_needs_a_review(self):
        before = {"providers/p.yaml": {"offerings": [{"model_id": "a", "prices": self.prices()}, {"model_id": "b", "prices": self.prices()}]}}
        after = {"providers/p.yaml": {"offerings": [{"model_id": "a", "prices": self.prices()}]}}
        reasons = []
        guard.compare_providers(before, after, reasons)
        self.assertTrue(any("b disappears" in r for r in reasons))

    def test_an_edited_model_file_needs_a_review(self):
        reasons = []
        guard.compare_models({"models/m.yaml": {"id": "m", "name": "M"}}, {"models/m.yaml": {"id": "m", "name": "N"}}, reasons)
        self.assertTrue(reasons)

    def test_the_repository_is_safe_against_itself(self):
        out = subprocess.run([sys.executable, str(REPO / "tools" / "guard.py"), "--base", "HEAD"], cwd=REPO, capture_output=True, text=True)
        self.assertIn(out.returncode, (0, 2), out.stderr)


if __name__ == "__main__":
    unittest.main()


class MistralTest(CatalogCopy):
    def card(self, slug):
        import gzip
        return gzip.decompress((FIXTURES / "mistral" / f"{slug}.html.gz").read_bytes()).decode("utf-8")

    def pages(self, **overrides):
        """Every card the index lists reads as the retired one, but those the
        test gives."""
        # Any card carries the navigation; the medium one is the index URL
        # itself, so another stands for it here.
        index = self.card("mistral-large-4-0")
        retired = self.card("codestral-25-01")
        known = {s: self.card(s) for s in ("mistral-medium-3-5-26-04", "mistral-large-4-0", "mistral-embed-23-12",
                                           "devstral-2-25-12", "ocr-4-1", "shieldstral-1-0")}
        known.update(overrides)

        class Pages(dict):
            def __missing__(self, url):
                slug = url.rsplit("/", 1)[-1]
                return known.get(slug, retired)

        return Pages(), index

    def setUp(self):
        super().setUp()
        self.saved_floor = refresh.MISTRAL_MIN_PRICED
        refresh.MISTRAL_MIN_PRICED = 3

    def tearDown(self):
        refresh.MISTRAL_MIN_PRICED = self.saved_floor
        super().tearDown()

    def test_reads_the_priced_cards_and_leaves_out_the_others(self):
        pages, index = self.pages()
        refresh.scrape_mistral("2026-10-29", pages=pages, index=index)
        offers = {o["model_id"]: o for o in self.provider("mistral")["offerings"]}
        self.assertEqual(sorted(offers), ["mistral-embed-2312", "mistral-large-4", "mistral-medium-3-5"])

        medium = offers["mistral-medium-3-5"]
        self.assertEqual((medium["prices"]["input"], medium["prices"]["output"]), (1.5, 7.5))
        self.assertEqual(medium["context_window"]["value"], 262144)
        # Labelled entries: the cached price is read apart.
        self.assertEqual(offers["mistral-large-4"]["prices"]["cached_input"], 0.07)
        # Embeddings: priced on input only, typed as such.
        self.assertEqual(offers["mistral-embed-2312"]["prices"]["output"], 0.0)
        embed = catalog.load_yaml(self.root / "models" / "mistral-embed-2312.yaml")
        self.assertEqual(embed["kind"], "embeddings")

    def test_refuses_an_unreadable_price(self):
        page = self.card("mistral-medium-3-5-26-04").replace('\\"price\\":1.5', '\\"price\\":\\"cheap\\"', 1)
        pages, index = self.pages(**{"mistral-medium-3-5-26-04": page})
        with self.assertRaises(refresh.ParseError):
            refresh.scrape_mistral("2026-10-29", pages=pages, index=index)

    def test_reads_a_card_with_its_price_among_other_props(self):
        # Since October 31, 2026 the price block follows a heading in its
        # component's props.
        card = refresh.mistral_card(self.card("codestral-25-08"), "codestral-25-08")
        self.assertEqual(card["names"], ["codestral-2508", "codestral-latest"])
        self.assertEqual(card["prices"], {"input": 0.3, "output": 0.9})

    def test_leaves_out_a_card_badged_deprecated_by_type(self):
        # The badge names its type rather than saying it in its text.
        self.assertIsNone(refresh.mistral_card(self.card("mistral-large-3-25-12"), "mistral-large-3-25-12"))

    def test_refuses_a_shrunken_list(self):
        refresh.MISTRAL_MIN_PRICED = 10
        pages, index = self.pages()
        with self.assertRaises(refresh.ParseError):
            refresh.scrape_mistral("2026-10-29", pages=pages, index=index)
