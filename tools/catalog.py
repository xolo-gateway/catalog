#!/usr/bin/env python3
"""Validate the catalog and build its JSON release.

    python3 tools/catalog.py validate [--max-age-days N]
    python3 tools/catalog.py build --version VERSION [--output dist/catalog.json]

validate checks every file against its schema and the references between
files, and flags values older than --max-age-days (a warning, not a
failure: an old value may still be right, it only needs checking).
"""

import argparse
import datetime
import json
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schema"

CATALOG_KIND = "xolo.catalog"
CATALOG_FORMAT = "1"


def load_yaml(path):
    with path.open(encoding="utf-8") as f:
        # Dates stay strings: the schema checks their format, and the JSON
        # build must not turn them into another type.
        return yaml.load(f, Loader=StringDatesLoader)


class StringDatesLoader(yaml.SafeLoader):
    pass


StringDatesLoader.add_constructor(
    "tag:yaml.org,2002:timestamp",
    lambda loader, node: loader.construct_scalar(node),
)


def validators():
    registry = Registry()
    schemas = {}
    for path in SCHEMAS.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource).with_resource(path.name, resource)
        schemas[path.name] = schema
    return {
        name: Draft202012Validator(schemas[name], registry=registry, format_checker=FormatChecker())
        for name in ("model.schema.json", "provider.schema.json")
    }


def provenances(value, path=""):
    """Yields every provenance block of value, with its path."""
    if isinstance(value, dict):
        if "status" in value and "date" in value:
            yield path, value
        for key, child in value.items():
            yield from provenances(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from provenances(child, f"{path}[{i}]")


def load_catalog():
    models = {}
    providers = {}
    for path in sorted((ROOT / "models").glob("*.yaml")):
        models[path] = load_yaml(path)
    for path in sorted((ROOT / "providers").glob("*.yaml")):
        providers[path] = load_yaml(path)
    return models, providers


def validate(max_age_days):
    checks = validators()
    models, providers = load_catalog()
    errors = []
    warnings = []

    def check(path, doc, validator):
        for error in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
            where = "/".join(str(p) for p in error.path) or "(root)"
            errors.append(f"{path.relative_to(ROOT)}: {where}: {error.message}")

    for path, doc in models.items():
        check(path, doc, checks["model.schema.json"])
        if isinstance(doc, dict) and doc.get("id") != path.stem:
            errors.append(f"{path.relative_to(ROOT)}: id {doc.get('id')!r} does not match the file name")
        params = (doc or {}).get("params") or {}
        if params.get("total") and params.get("active") and params["active"]["value"] > params["total"]["value"]:
            errors.append(f"{path.relative_to(ROOT)}: active parameters exceed the total")

    model_ids = {doc.get("id") for doc in models.values() if isinstance(doc, dict)}
    for path, doc in providers.items():
        check(path, doc, checks["provider.schema.json"])
        if not isinstance(doc, dict):
            continue
        if doc.get("id") != path.stem:
            errors.append(f"{path.relative_to(ROOT)}: id {doc.get('id')!r} does not match the file name")
        offered = set()
        for i, offering in enumerate(doc.get("offerings") or []):
            if offering.get("model") not in model_ids:
                errors.append(f"{path.relative_to(ROOT)}: offerings[{i}]: unknown model {offering.get('model')!r}")
            api_type = offering.get("api_type")
            if api_type and api_type != (doc.get("xolo") or {}).get("type") and not offering.get("base_url"):
                errors.append(f"{path.relative_to(ROOT)}: offerings[{i}]: api_type {api_type!r} differs from the provider's and needs its base_url")
            if offering.get("model_id") in offered:
                errors.append(f"{path.relative_to(ROOT)}: offerings[{i}]: model_id {offering.get('model_id')!r} is listed twice")
            offered.add(offering.get("model_id"))
        for plan in doc.get("plans") or []:
            for c in plan.get("constraints") or []:
                for model_id in c.get("model_budgets") or {}:
                    if model_id not in offered:
                        errors.append(f"{path.relative_to(ROOT)}: plan {plan.get('id')!r}: budget for {model_id!r}, which is not offered")

    today = datetime.date.today()
    for path, doc in {**models, **providers}.items():
        for where, block in provenances(doc):
            try:
                age = (today - datetime.date.fromisoformat(str(block["date"]))).days
            except ValueError:
                continue
            if age > max_age_days:
                warnings.append(f"{path.relative_to(ROOT)}: {where}: dated {block['date']}, {age} days old")

    for w in warnings:
        # GitHub Actions shows it as an annotation; elsewhere it is a line.
        print(f"::warning::{w}")
    for e in errors:
        print(f"::error::{e}")
    print(f"{len(models)} models, {len(providers)} providers, {len(errors)} errors, {len(warnings)} stale values")
    return 1 if errors else 0


def build(version, output):
    models, providers = load_catalog()
    catalog = {
        "kind": CATALOG_KIND,
        "format": CATALOG_FORMAT,
        "version": version,
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "license": "ODbL-1.0",
        "models": [models[p] for p in sorted(models)],
        "providers": [providers[p] for p in sorted(providers)],
    }
    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output} ({len(catalog['models'])} models, {len(catalog['providers'])} providers)")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--max-age-days", type=int, default=180)
    b = sub.add_parser("build")
    b.add_argument("--version", required=True)
    b.add_argument("--output", default="dist/catalog.json")
    args = parser.parse_args()
    if args.command == "validate":
        return validate(args.max_age_days)
    return build(args.version, args.output)


if __name__ == "__main__":
    sys.exit(main())
