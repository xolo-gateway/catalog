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
    scope: model
    model_budgets: {deepseek-v4.1-flash: {value_budget: 16.0}, beam-free: {}}
```

Plan constraints use the vocabulary of Xolo's subscription plans: rolling windows with a token or value budget (in the provider's currency), counted per provider or per model, with per-model overrides, and concurrency limits.

## Daily refresh

The prices, model lists and plan limits a provider publishes change often. `tools/refresh.py` re-reads them every day, from the provider's own pages or API, with one scraper per source and no language model in the loop: every value it writes comes from the page its `source` names.

| Source | Reads | Refreshes |
| --- | --- | --- |
| `opencode-go` | the tables of https://opencode.ai/docs/go/ | offerings, API types, prices (tiers and peak hours included), plans and their per-model budgets; adds the models OpenCode Go starts serving |
| `openrouter` | https://openrouter.ai/api/v1/models | prices and windows of the offerings already in `providers/openrouter.yaml` |

A value that did not change keeps its date, unless it is older than 30 days, when it is confirmed with the day's date. The `refresh` workflow runs it daily and opens one pull request when a provider changed something: a maintainer checks it against the sources before it is merged and released. A model a provider starts serving gets a file with its name and vendor only. Its windows, capabilities and parameter counts are left for a contributor to source, as no scraper can read them reliably from heterogeneous vendor pages.

A scraper that no longer parses its page fails the run with an annotation, rather than writing a partial result.

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

## License

The catalog is made available under the [Open Database License (ODbL) 1.0](LICENSE). You may use, share and adapt it, provided you attribute it to the Xolo model catalog and share a database adapted from it under the same license. The scripts in `tools/` are under the same terms.
