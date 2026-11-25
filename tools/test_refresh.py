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


def opencode_models():
    return json.loads((FIXTURES / "opencode-go-models.json").read_text(encoding="utf-8"))


def listed(document, model_id):
    return next(m for m in document["data"] if m["id"] == model_id)


class OpenCodeGoTest(CatalogCopy):
    def scrape(self, models=None, page=None, today="2026-11-23"):
        refresh.scrape_opencode_go(
            today,
            page=page if page is not None else opencode_page(),
            models=json.dumps(models if models is not None else opencode_models()),
        )
        return self.provider("opencode-go")

    def refuses(self, change):
        models = opencode_models()
        change(models)
        with self.assertRaises(refresh.ParseError):
            self.scrape(models)

    def test_reads_every_offering_and_plan(self):
        p = self.scrape()
        offerings = {o["model_id"]: o for o in p["offerings"]}
        self.assertEqual(len(offerings), 33)
        self.assertEqual([plan["id"] for plan in p["plans"]], ["go", "go-plus", "go-teams"])
        self.assertEqual([plan["price"]["amount"] for plan in p["plans"]], [10.0, 40.0, 20.0])
        # One window per period, shared by every model, each weighing by its
        # own limit; the window holds the plan's most common limit.
        five_hours = p["plans"][0]["constraints"][0]
        self.assertEqual((five_hours["scope"], five_hours["label"]), ("shared", "5h"))
        limits = [b["value_budget"] for b in five_hours["model_budgets"].values() if b]
        self.assertEqual(five_hours["value_budget"], max(limits, key=lambda v: (limits.count(v), v)))
        # 20% of the published monthly limit; a model with none is free.
        self.assertEqual(five_hours["model_budgets"]["deepseek-v4.1-flash"], {"value_budget": 20.0})
        self.assertEqual(five_hours["model_budgets"]["beam-free"], {})

        deepseek = offerings["deepseek-v4.1-flash"]
        self.assertEqual((deepseek["api_type"], deepseek["base_url"]), ("openai", "https://opencode.ai/inference/go/openai/v1"))
        self.assertEqual((deepseek["prices"]["input"], deepseek["prices"]["output"]), (0.15, 0.6))
        peak = deepseek["prices"]["peak"][0]
        self.assertEqual(peak["hours"], ["01:00-04:00", "06:00-10:00"])
        self.assertEqual((peak["input"], peak["output"], peak["cached_input"]), (0.3, 1.2, 0.006))

        haiku = offerings["claude-haiku-5-5"]
        self.assertEqual(haiku["api_type"], "anthropic")
        self.assertEqual(haiku["prices"]["tiers"], [
            {"above_input_tokens": 100_000, "input": 0.5, "cached_input": 0.05, "cache_write": 0.625, "output": 2.5},
        ])
        # Priced by how long the cache lives: the shortest, the default.
        self.assertEqual(offerings["gpt-6-luna"]["prices"]["cache_write"], 0.125)
        self.assertEqual(offerings["gpt-6-luna"]["api_type"], "openai-responses")

        # Listed with Google's Interactions API first: read through Gemini's.
        argon = offerings["gemini-4-argon"]
        self.assertEqual(argon["api_type"], "gemini")
        # The discount ends on 2026-12-01, when a factor of 2 starts.
        self.assertEqual((argon["prices"]["input"], argon["prices"]["output"]), (2.0, 10.0))

    def test_reads_an_embedding_model_and_leaves_out_a_decision_model(self):
        p = self.scrape()
        offerings = {o["model_id"]: o for o in p["offerings"]}
        # Embeddings: OpenAI's API, priced on input only, typed as such.
        embed = offerings["qwen3-embedding-8b"]
        self.assertEqual((embed["api_type"], embed["base_url"]), ("openai", "https://opencode.ai/inference/go/openai/v1"))
        self.assertEqual((embed["prices"]["input"], embed["prices"]["output"]), (0.01, 0.0))
        self.assertEqual(catalog.load_yaml(self.root / "models" / "qwen3-embedding-8b.yaml")["kind"], "embeddings")
        self.assertEqual(p["plans"][0]["constraints"][2]["model_budgets"]["qwen3-embedding-8b"], {"value_budget": 10.0})
        self.assertEqual(
            sorted(m for m in offerings if "embedding" in m),
            ["gemini-embedding-2", "qwen3-embedding-8b", "text-embedding-3-small"],
        )
        self.assertEqual(catalog.load_yaml(self.root / "models" / "text-embedding-3-small.yaml")["vendor"], "OpenAI")
        # A decision model has no API type in the catalog: no offering, no
        # budget, no model file.
        for model_id in ("jev-1.14", "gemini-decision-1", "gpt-6-luna-decision"):
            self.assertNotIn(model_id, offerings)
            for plan in p["plans"]:
                for constraint in plan["constraints"]:
                    self.assertNotIn(model_id, constraint["model_budgets"])
            self.assertFalse((self.root / "models" / f"{model_id}.yaml").exists())

    def test_refuses_an_unknown_model_type(self):
        self.refuses(lambda d: listed(d, "glm-5.3").update(type="rerank"))

    def test_refuses_a_chat_model_listed_with_the_embeddings_api_only(self):
        def change(d):
            m = listed(d, "glm-5.3")
            m["api"] = {"embeddings": m["api"]["chat-completions"]}
        self.refuses(change)

    def test_refuses_an_embedding_model_priced_on_output(self):
        self.refuses(lambda d: listed(d, "qwen3-embedding-8b")["cost"]["text"]["output"].update(price=0.01))

    def test_applies_a_dated_factor_once_it_starts(self):
        argon = {o["model_id"]: o for o in self.scrape(today="2026-12-01")["offerings"]}["gemini-4-argon"]
        self.assertEqual((argon["prices"]["input"], argon["prices"]["output"]), (4.0, 20.0))

    def test_is_deterministic(self):
        first = self.scrape()
        self.assertEqual(self.scrape(), first)

    def test_refuses_an_unreadable_price(self):
        self.refuses(lambda d: listed(d, "glm-5.3")["cost"]["text"]["input"].update(price="1.40 USD"))

    def test_refuses_an_unknown_cost_field(self):
        self.refuses(lambda d: listed(d, "glm-5.3")["cost"]["text"]["input"].update(batch=0.5))

    def test_refuses_an_unknown_override_condition(self):
        def change(d):
            listed(d, "grok-4.7")["cost"]["text"]["output"]["overrides"][0]["when"] = {"tokens": {"gt": 200000}}
        self.refuses(change)

    def test_refuses_an_unknown_multiplier(self):
        def change(d):
            listed(d, "deepseek-v4.1-flash")["cost"]["multipliers"][0]["when"] = {"region": "eu"}
        self.refuses(change)

    def test_refuses_an_unknown_api(self):
        def change(d):
            m = listed(d, "glm-5.3")
            m["api"] = {"realtime": m["api"]["chat-completions"]}
        self.refuses(change)

    def test_refuses_a_model_missing_a_plan(self):
        self.refuses(lambda d: listed(d, "glm-5.3")["go"]["monthly_usage"].pop("go-plus"))

    def test_refuses_a_plan_the_page_does_not_price(self):
        with self.assertRaises(refresh.ParseError):
            self.scrape(page=opencode_page().replace("Go Plus", "Go Max"))

    def test_a_failing_list_writes_nothing(self):
        before = sorted(p.name for p in (self.root / "models").iterdir())
        provider = (self.root / "providers" / "opencode-go.yaml").read_text()
        models = opencode_models()
        models["data"].append({"id": "brand-new", "type": "chat", "api": {}})
        with self.assertRaises(refresh.ParseError):
            self.scrape(models)
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

    def test_a_provider_in_an_incident_still_counts(self):
        # Its status flips within minutes: leaving it out would move the
        # median between two runs a minute apart.
        doc = json.loads(openrouter_endpoints((0.1, 0.5), (1.4, 4.4)))
        doc["data"]["endpoints"] += json.loads(openrouter_endpoints((1.2, 4.0), status=-2))["data"]["endpoints"]
        prices = refresh.openrouter_median_prices("m", json.dumps(doc))
        self.assertEqual((prices["input"], prices["output"]), (1.2, 4.0))

    def test_the_median_of_an_even_count_is_the_mean_of_the_middle_two(self):
        prices = refresh.openrouter_median_prices("m", openrouter_endpoints((0.1, 0.5), (0.3, 1.5)))
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
    def page(self):
        import gzip
        return gzip.decompress((FIXTURES / "mistral-models.html.gz").read_bytes()).decode("utf-8")

    def listed(self):
        """The model objects of the frozen page, to change and wrap again."""
        return refresh.mistral_listed(self.page())

    def wrap(self, models):
        """A page streaming models the way the docs do."""
        payload = json.dumps(models, separators=(",", ":"))
        return "<script>self.__next_f.push(" + json.dumps([1, payload]) + ")</script>"

    def models(self, value):
        """Every model object under value, the ones a model holds (the model
        replacing it, the models related to it) included."""
        if isinstance(value, dict):
            if "modelIds" in value:
                yield value
            for child in value.values():
                yield from self.models(child)
        elif isinstance(value, list):
            for child in value:
                yield from self.models(child)

    def copies(self, listed, slug):
        copies = [m for m in self.models(listed) if m["slug"] == slug]
        self.assertTrue(copies)
        return copies

    def change(self, listed, slug, **fields):
        """Changes every copy of a model."""
        for m in self.copies(listed, slug):
            m.update(fields)
        return listed

    def scrape(self, listed=None, today="2026-11-25"):
        refresh.scrape_mistral(today, page=self.wrap(listed) if listed is not None else self.page())
        return {o["model_id"]: o for o in self.provider("mistral")["offerings"]}

    def refuses(self, listed):
        with self.assertRaises(refresh.ParseError):
            self.scrape(listed)

    def test_reads_the_priced_models_and_leaves_out_the_others(self):
        offers = self.scrape()
        for model_id in ("codestral-2508", "mistral-embed-2312", "mistral-large-4", "mistral-small-2603", "zai-glm-5-3"):
            self.assertIn(model_id, offers)
        # Retired, priced by the page or the minute, or called through
        # another API: left out.
        for model_id in ("mistral-next", "mistral-ocr-4-1", "voxtral-small-2507", "mistral-moderation-2603"):
            self.assertNotIn(model_id, offers)

        large = offers["mistral-large-4"]
        self.assertEqual((large["prices"]["input"], large["prices"]["output"]), (0.7, 2.1))
        # Labelled entries: the cached price is read apart.
        self.assertEqual(large["prices"]["cached_input"], 0.07)
        self.assertEqual(large["context_window"]["value"], 1_000_000)
        # A hosted third-party model is the catalog's own.
        self.assertEqual(offers["zai-glm-5-3"]["model"], "glm-5.3")
        # Embeddings: priced on input only, typed as such.
        embed = offers["mistral-embed-2312"]["prices"]
        self.assertEqual((embed["input"], embed["output"]), (0.1, 0.0))
        self.assertEqual(catalog.load_yaml(self.root / "models" / "mistral-embed-2312.yaml")["kind"], "embeddings")

    def test_reads_the_wrapped_models_like_the_page(self):
        first = self.scrape()
        self.assertEqual(self.scrape(self.listed()), first)

    def test_refuses_an_unreadable_price(self):
        listed = self.listed()
        for m in self.copies(listed, "mistral-large-4-0"):
            m["pricing"][0]["priceUsd"] = "cheap"
        self.refuses(listed)

    def test_refuses_an_unknown_price_label(self):
        listed = self.listed()
        for m in self.copies(listed, "mistral-large-4-0"):
            m["pricing"][1]["label"] = "Batch input"
        self.refuses(listed)

    def test_refuses_a_price_label_read_twice(self):
        listed = self.listed()
        for m in self.copies(listed, "mistral-large-4-0"):
            m["pricing"][1]["label"] = "Input"
        self.refuses(listed)

    def test_reads_an_unlabelled_price_as_its_side(self):
        listed = self.listed()
        for m in self.copies(listed, "mistral-large-4-0"):
            m["pricing"][0]["label"] = None
        self.assertEqual(self.scrape(listed)["mistral-large-4"]["prices"]["input"], 0.7)

    def test_reads_a_free_model_at_zero_whatever_its_price_entries(self):
        listed = self.listed()
        self.change(listed, "mistral-large-4-0", isFree=True, pricing=None)
        prices = self.scrape(listed)["mistral-large-4"]["prices"]
        self.assertEqual((prices["input"], prices["output"]), (0.0, 0.0))
        self.assertNotIn("cached_input", prices)

    def test_refuses_a_model_not_saying_whether_it_is_free(self):
        self.refuses(self.change(self.listed(), "mistral-large-4-0", isFree=None))

    def test_reads_nothing_else_of_a_retired_model(self):
        listed = self.change(self.listed(), "mistral-next", name=None, modelIds=["Not An ID"])
        self.assertIn("mistral-large-4", self.scrape(listed))

    def test_refuses_an_unknown_status(self):
        self.refuses(self.change(self.listed(), "mistral-large-4-0", status="Sunset"))

    def test_refuses_a_model_stated_twice_differently(self):
        listed = self.listed()
        copy = json.loads(json.dumps(next(m for m in listed if m["slug"] == "mistral-large-4-0")))
        copy["pricing"][0]["priceUsd"] = 7
        self.refuses(listed + [copy])

    def test_leaves_out_a_model_priced_in_another_unit(self):
        listed = self.listed()
        for m in self.copies(listed, "mistral-large-4-0"):
            m["pricing"].append({"direction": "input", "priceUsd": 0.004, "unit": "/Min", "label": "Input"})
        self.assertNotIn("mistral-large-4", self.scrape(listed))

    def test_leaves_out_a_retired_model(self):
        self.assertNotIn("mistral-large-4", self.scrape(self.change(self.listed(), "mistral-large-4-0", status="Retired")))

    def test_keeps_a_deprecated_model_as_last_read_and_adds_no_new_one(self):
        before = {o["model_id"]: o for o in self.provider("mistral")["offerings"]}
        self.assertIn("mistral-large-4", before)
        listed = self.change(self.listed(), "mistral-large-4-0", status="Deprecated")
        for m in self.copies(listed, "mistral-large-4-0"):
            m["pricing"][0]["priceUsd"] = 70
        self.change(listed, "codestral-25-08", status="Deprecated", modelIds=["codestral-2599"])
        offers = self.scrape(listed)
        self.assertEqual(offers["mistral-large-4"], before["mistral-large-4"])
        self.assertNotIn("codestral-2599", offers)
        self.assertFalse((self.root / "models" / "codestral-2599.yaml").exists())

    def test_two_deprecated_models_may_name_the_same_model(self):
        listed = self.change(self.listed(), "mistral-large-4-0", status="Deprecated")
        self.change(listed, "codestral-25-08", status="Deprecated", modelIds=["mistral-large-4"])
        self.assertIn("mistral-large-4", self.scrape(listed))

    def test_refuses_two_priced_models_naming_the_same_model(self):
        self.refuses(self.change(self.listed(), "codestral-25-08", modelIds=["mistral-large-4"]))

    def test_refuses_a_list_read_mostly_as_deprecated(self):
        # A status read wrong on every model would otherwise write the last
        # prices back as a success.
        listed = self.listed()
        for m in self.models(listed):
            if m["status"] != "Retired":
                m["status"] = "Deprecated"
        self.refuses(listed)

    def test_refuses_a_shrunken_list(self):
        listed = self.listed()
        for m in listed:
            m.update(replacedBy=None, relatedModels=[])
        # The priced models first: only the list's length is short.
        priced = [m for m in listed if refresh.mistral_model(m) and "prices" in refresh.mistral_model(m)]
        slugs = sorted({m["slug"] for m in priced})[:refresh.MISTRAL_MIN_LISTED - 1]
        self.assertGreaterEqual(len(slugs), refresh.MISTRAL_MIN_PRICED)
        self.refuses([m for m in listed if m["slug"] in slugs])

    def test_refuses_a_page_without_models(self):
        with self.assertRaises(refresh.ParseError):
            refresh.scrape_mistral("2026-11-25", page="<html>" + "x" * 2000 + "</html>")
