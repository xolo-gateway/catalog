#!/usr/bin/env python3
"""Decide whether a refresh can be merged without a review.

    python3 tools/guard.py --base origin/main [--github-output FILE]

Compares the working tree with the catalog at the base revision. A change
a scraper could make by misreading a page is held for review: a price that
moves by more than MAX_PRICE_CHANGE, a price appearing or vanishing, an
offering, provider or plan disappearing, an offering changing API, a plan
budget moving by more than MAX_BUDGET_CHANGE or turning limited/unlimited,
too many offerings appearing at once, or an existing model file edited
(scrapers only create stubs). Anything else is safe to merge.

Exits 0 when safe, 2 when a review is needed; prints the reasons either way.
"""

import argparse
import pathlib
import subprocess
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from catalog import ROOT, StringDatesLoader, load_yaml  # noqa: E402

MAX_PRICE_CHANGE = 0.30
MAX_BUDGET_CHANGE = 0.30
MAX_NEW_OFFERINGS = 5
PRICE_FIELDS = ("input", "cached_input", "cache_write", "output")


def base_files(base, directory):
    """The YAML files of directory at the base revision, by path."""
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", f"{base}:{directory}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if listing.returncode != 0:
        return {}
    files = {}
    for name in listing.stdout.split():
        if not name.endswith(".yaml"):
            continue
        content = subprocess.run(
            ["git", "show", f"{base}:{directory}/{name}"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        files[f"{directory}/{name}"] = yaml.load(content, Loader=StringDatesLoader)
    return files


def head_files(directory):
    return {f"{directory}/{p.name}": load_yaml(p) for p in sorted((ROOT / directory).glob("*.yaml"))}


def relative(before, after):
    if before == after:
        return 0.0
    if not before:
        return float("inf")
    return abs(after - before) / abs(before)


def compare_prices(where, before, after, reasons):
    for field in PRICE_FIELDS:
        b, a = before.get(field), after.get(field)
        if (b is None) != (a is None):
            reasons.append(f"{where}: {field} {'appears' if b is None else 'vanishes'} ({b} → {a})")
        elif b is not None and (b == 0) != (a == 0):
            reasons.append(f"{where}: {field} turns {'free' if a == 0 else 'paid'} ({b} → {a})")
        elif b is not None and relative(b, a) > MAX_PRICE_CHANGE:
            reasons.append(f"{where}: {field} moves by {relative(b, a):.0%} ({b} → {a})")
    for kind in ("tiers", "peak"):
        b, a = before.get(kind) or [], after.get(kind) or []
        if len(b) != len(a):
            reasons.append(f"{where}: {len(b)} {kind} → {len(a)}")
            continue
        for i, (bt, at) in enumerate(zip(b, a)):
            if kind == "tiers" and bt.get("above_input_tokens") != at.get("above_input_tokens"):
                reasons.append(f"{where}: tier {i} threshold changes")
            if kind == "peak" and (bt.get("hours") != at.get("hours") or bt.get("days") != at.get("days")):
                reasons.append(f"{where}: peak hours change")
            compare_prices(f"{where} {kind}[{i}]", bt, at, reasons)


def compare_providers(before, after, reasons):
    for path, old in before.items():
        new = after.get(path)
        if new is None:
            reasons.append(f"{path}: the provider disappears")
            continue
        if old.get("currency") != new.get("currency"):
            reasons.append(f"{path}: currency {old.get('currency')} → {new.get('currency')}")
        if old.get("xolo") != new.get("xolo"):
            reasons.append(f"{path}: default API changes")

        old_offers = {o["model_id"]: o for o in old.get("offerings") or []}
        new_offers = {o["model_id"]: o for o in new.get("offerings") or []}
        for model_id in sorted(set(old_offers) - set(new_offers)):
            reasons.append(f"{path}: offering {model_id} disappears")
        added = sorted(set(new_offers) - set(old_offers))
        if len(added) > MAX_NEW_OFFERINGS:
            reasons.append(f"{path}: {len(added)} offerings appear at once ({', '.join(added)})")
        for model_id in sorted(set(old_offers) & set(new_offers)):
            o, n = old_offers[model_id], new_offers[model_id]
            where = f"{path}: {model_id}"
            if (o.get("api_type"), o.get("base_url"), o.get("model")) != (n.get("api_type"), n.get("base_url"), n.get("model")):
                reasons.append(f"{where}: API or model changes")
            compare_prices(where, o.get("prices") or {}, n.get("prices") or {}, reasons)

        old_plans = {p["id"]: p for p in old.get("plans") or []}
        new_plans = {p["id"]: p for p in new.get("plans") or []}
        for plan_id in sorted(set(old_plans) - set(new_plans)):
            reasons.append(f"{path}: plan {plan_id} disappears")
        for plan_id in sorted(set(old_plans) & set(new_plans)):
            o, n = old_plans[plan_id], new_plans[plan_id]
            if (o.get("price") or {}).get("amount") != (n.get("price") or {}).get("amount"):
                reasons.append(f"{path}: plan {plan_id} price changes")
            old_constraints = {c["label"]: c for c in o.get("constraints") or []}
            new_constraints = {c["label"]: c for c in n.get("constraints") or []}
            if set(old_constraints) != set(new_constraints):
                reasons.append(f"{path}: plan {plan_id} constraints change")
                continue
            for label, oc in old_constraints.items():
                nc = new_constraints[label]
                if (oc.get("duration"), oc.get("scope"), oc.get("kind")) != (nc.get("duration"), nc.get("scope"), nc.get("kind")):
                    reasons.append(f"{path}: plan {plan_id} {label} window changes")
                ob, nb = oc.get("model_budgets") or {}, nc.get("model_budgets") or {}
                for model_id in sorted(set(ob) - set(nb)):
                    reasons.append(f"{path}: plan {plan_id} {label} loses the budget of {model_id}")
                for model_id in sorted(set(ob) & set(nb)):
                    bv, av = (ob[model_id] or {}).get("value_budget"), (nb[model_id] or {}).get("value_budget")
                    if (bv is None) != (av is None):
                        reasons.append(f"{path}: plan {plan_id} {label} {model_id} turns {'limited' if bv is None else 'unlimited'}")
                    elif bv is not None and relative(bv, av) > MAX_BUDGET_CHANGE:
                        reasons.append(f"{path}: plan {plan_id} {label} {model_id} budget moves by {relative(bv, av):.0%}")


def compare_models(before, after, reasons):
    for path, old in before.items():
        new = after.get(path)
        if new is None:
            reasons.append(f"{path}: the model file disappears")
        elif new != old:
            reasons.append(f"{path}: an existing model file is edited")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--github-output")
    args = parser.parse_args()

    reasons = []
    compare_providers(base_files(args.base, "providers"), head_files("providers"), reasons)
    compare_models(base_files(args.base, "models"), head_files("models"), reasons)

    safe = not reasons
    for reason in reasons:
        print(f"::warning::review needed: {reason}")
    print("safe to merge without review" if safe else f"{len(reasons)} change(s) need a review")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as out:
            out.write(f"safe={'true' if safe else 'false'}\n")
            out.write("reasons<<EOF\n" + "\n".join(f"- {r}" for r in reasons) + "\nEOF\n")
    return 0 if safe else 2


if __name__ == "__main__":
    sys.exit(main())
