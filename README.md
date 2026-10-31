# Xolo model catalog

An open database of LLMs and of the providers that serve them, maintained by the [Xolo](https://github.com/xolo-gateway/xolo) project. Xolo imports it to pre-fill and update the configuration of a provider's models, but the data is plain YAML and JSON for anyone to use.

Every figure carries its provenance: a source URL, the date it was read, and whether it is **published** by the vendor or provider, **measured** by a contributor, or **estimated**. An estimate is allowed, because an energy estimate needs a parameter count even for a closed model, but it states its reasoning and is never presented as a published number.

## Layout

```
models/<id>.yaml       one upstream model, whoever serves it
providers/<id>.yaml    one provider: its offerings (the models it serves) and its plans
schema/                JSON schemas of both files
tools/catalog.py       validation and build
```

### Models

```yaml
id: glm-5.3
name: GLM-5.3
family: glm-5
vendor: Z.ai (Zhipu AI)
open_weights: true
context_window:
  value: 1048576
  status: published
  source: https://docs.z.ai/guides/llm/glm-5.3
  date: '2026-10-29'
params:
  total: {value: 753000000000, status: published, source: https://huggingface.co/zai-org/GLM-5.3, date: '2026-10-29'}
  active:
    value: 40000000000
    status: estimated
    source: https://huggingface.co/zai-org/GLM-5.3/blob/main/config.json
    date: '2026-10-29'
    note: Z.ai does not state active parameters for this release. Its config.json has the same MoE
      layout as GLM-5, for which Z.ai states 40B active.
```

### Providers

A provider lists its **offerings**: one per model it serves, with the model ID it expects, the API it is called through when that is not its default, and its prices per million tokens in its currency. `input`, `cached_input` and `output` are the base tier. Prices that vary with the prompt size (`tiers`) or the time of day (`peak`) are kept as published.

```yaml
id: opencode-go
name: OpenCode Go
website: https://opencode.ai/go
currency: USD
xolo: {type: openai, base_url: https://opencode.ai/inference/go/v1}
offerings:
- model: deepseek-v4.1-flash
  model_id: deepseek-v4.1-flash
  api_type: openai
  base_url: https://opencode.ai/inference/go/openai/v1
  prices:
    input: 0.15
    output: 0.6
    cached_input: 0.003
    peak:
    - {hours: [01:00-04:00, 06:00-10:00], timezone: UTC, days: [mon, tue, wed, thu, fri], input: 0.3, output: 1.2, cached_input: 0.006}
    status: published
    source: https://opencode.ai/docs/go/
    date: '2026-10-29'
plans:
- id: go
  name: Go
  price: {amount: 10, period: month}
  constraints:
  - kind: rolling_window
    label: 5h
    duration: 5h
    scope: shared
    value_budget: 3.0
    model_budgets: {deepseek-v4.1-flash: {value_budget: 16.0}, glm-5.3: {value_budget: 3.0}, beam-free: {}}
```

Plan constraints use the vocabulary of Xolo's subscription plans: rolling windows with a token or value budget (in the provider's currency), and concurrency limits. A window is counted for the whole provider (`provider`), per model with a budget each (`model`), or once for every model with each model weighing by its own budget (`shared`): spending a model's whole budget then fills the window. OpenCode Go meters its windows that way.

## Daily refresh

The prices, model lists and plan limits a provider publishes change often. `tools/refresh.py` re-reads them every day, from the provider's own pages or API, with one scraper per source and no language model in the loop: every value it writes comes from the page its `source` names.

| Source | Reads | Refreshes |
| --- | --- | --- |
| `opencode-go` | the tables of https://opencode.ai/docs/go/ | offerings, API types, prices (tiers and peak hours included), plans and their per-model budgets; adds the models OpenCode Go starts serving |
| `mistral` | the model cards of https://docs.mistral.ai/models | the models served through chat completions or embeddings and priced in tokens (base, cached and output prices, context size); retired, deprecated and non-token models are left out |
| `openrouter` | https://openrouter.ai/api/v1/models | prices and windows of the offerings already in `providers/openrouter.yaml` |

A value that did not change keeps its date, unless it is older than 30 days, when it is confirmed with the day's date. A model a provider starts serving gets a file with its name and vendor only: its windows, capabilities and parameter counts are left for a contributor to source, as no scraper can read them reliably from heterogeneous vendor pages.

### Safeguards

A misread page must never become a price. The `refresh` workflow runs every day and opens one pull request when a provider changed something. That pull request merges on its own only when every safeguard holds:

- **Strict reading.** Each scraper expects its tables, columns, cell formats, price qualifiers (tiers, peak hours, discounts) and endpoints exactly. Anything else (an unreadable price, a missing column, a model priced but without an endpoint, a row of the wrong length) fails that source, which then writes nothing at all.
- **Frozen pages.** The scrapers are tested on copies of the pages they read (`tools/fixtures`), including pages altered to break each rule.
- **Determinism.** A second run on the same day must read exactly the same.
- **Validation.** The catalog validates against its schemas and cross-references.
- **Plausibility guard** (`tools/guard.py`). The change is held for review when a price moves by more than 30%, appears, vanishes or turns free, when an offering, a plan or a provider disappears, when an offering changes API, when a plan budget moves by more than 30% or turns limited or unlimited, when more than 5 offerings appear at once, or when an existing model file is edited.

When a safeguard does not hold, the pull request stays open, its description says which safeguard failed and lists each implausible change, and any auto-merge an earlier run enabled is cancelled. Only the daily refresh pull request can merge on its own; every other pull request is merged by a maintainer.

## Contributing

1. Add or edit the YAML files. One model or one provider per file, so that a diff reads as one change.
2. Give every figure a source a reader can open, and the date you read it. Prefer the vendor's or provider's own page. A blog post, a benchmark or a third-party aggregator is acceptable as `measured` or `estimated`, never as `published`.
3. Run the validation:

   ```sh
   pip install -r tools/requirements.txt
   python3 tools/catalog.py validate
   ```

   It checks the schemas, the references between files, and flags values older than 180 days. A stale value is a warning: it may still be right, it only needs checking against its source.

Contributions are reviewed by the Xolo maintainers.

## Releases

Each `v*` tag publishes `catalog.json`, every model and provider in one file, as a GitHub release asset:

```
https://github.com/xolo-gateway/catalog/releases/latest/download/catalog.json
```

Its `version` is the tag, so that Xolo can record which release a model was imported from.

Xolo reads the latest release only, and follows a model's values only when the release's version differs from the one the model was imported with. So every push to `main` that changes `models/` or `providers/` publishes a release on its own (`.github/workflows/publish.yml`), whether it is a daily refresh merged on its own or a pull request merged by a maintainer. A release is named after its date in UTC, `vYYYY.M.D`, then `vYYYY.M.D.2`, `vYYYY.M.D.3` and so on for the later releases of the same day. A change outside `models/` and `providers/`, to `tools/` or `schema/` for instance, publishes nothing: a maintainer releases it by pushing a tag named the same way, `git tag vYYYY.M.D[.N] && git push origin vYYYY.M.D[.N]`.

## License

The catalog is made available under the [Open Database License (ODbL) 1.0](LICENSE). You may use, share and adapt it, provided you attribute it to the Xolo model catalog and share a database adapted from it under the same license. The scripts in `tools/` are under the same terms.
