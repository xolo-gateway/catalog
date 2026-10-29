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


class OpenRouterTest(CatalogCopy):
    def document(self):
        return json.loads((FIXTURES / "openrouter-models.json").read_text(encoding="utf-8"))

    def test_refreshes_the_listed_offerings(self):
        refresh.scrape_openrouter("2026-10-29", document=json.dumps(self.document()))
        offers = self.provider("openrouter")["offerings"]
        self.assertGreater(len(offers), 20)
        for o in offers:
            self.assertGreaterEqual(o["prices"]["input"], 0)

    def test_refuses_an_unreadable_price(self):
        doc = self.document()
        listed = {o["model_id"] for o in self.provider("openrouter")["offerings"]}
        for m in doc["data"]:
            if m["id"] in listed:
                m["pricing"]["prompt"] = "about a dollar"
                break
        with self.assertRaises(refresh.ParseError):
            refresh.scrape_openrouter("2026-10-29", document=json.dumps(doc))

    def test_refuses_a_truncated_list(self):
        doc = self.document()
        doc["data"] = doc["data"][:10]
        with self.assertRaises(refresh.ParseError):
            refresh.scrape_openrouter("2026-10-29", document=json.dumps(doc))


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
