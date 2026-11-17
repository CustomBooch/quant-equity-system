# quant

A point-in-time US equity research and paper-trading framework: plug in a dataset, build monthly features without look-ahead, score a verified
data release with a model, turn the scores into a target book, and send it to a broker through an order path with hard controls.

This repository holds the engineering only. It ships no data and an **example** model (`src/quant/models/definition.py`, a transparent factor blend)
with example book parameters (`src/quant/portfolio/model.py`). Replace both with your own.

```text
data-source plugins  ->  point-in-time store  ->  monthly features  ->  verified release  ->  scores  ->  target book  ->  tranche plan  ->  preflight  ->  engine  ->  broker
quant.data.sources       record/data/sources     quant.features       quant.core.release    models      portfolio        portfolio.plan     execution     execution    brokers/
```

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r requirements/core.lock.txt      # Windows: .venv\Scripts\pip
tools/py -m pytest -q                       # the test suite (no data needed; data-dependent tests skip with the reason)
tools/py -m quant                           # the command list
tools/py -m quant data list                 # registered data sources
```

## Adding a dataset

One file in `src/quant/data/sources/plugins/` plus one licence row: declare the dataset (entity, the date each value describes, the date it became
public, columns) and write `fetch`. The pull, the point-in-time checks, the store with a manifest, `quant data pull/check/show` and the monthly join
come with it. Walkthrough: [Adding a dataset](https://github.com/CustomBooch/quant-equity-system/wiki/Adding-a-dataset).

## What is in it

| Layer | Package | Holds |
| --- | --- | --- |
| 0 | `quant.core` | paths, statistics (Newey-West, bootstraps, Holm), Rust kernels with bit-identical Python references, the release contract, licences |
| 1 | `quant.data` | data-source plugins and store; SEC EDGAR, Yahoo, Capital IQ export readers, the Alpaca asset list |
| 2 | `quant.features` | point-in-time monthly joins, price features from daily bars, the bridge-month fundamentals fill |
| 3 | `quant.models` | scoring a verified release with the model definition, with input-coverage rails |
| 4 | `quant.portfolio` | book parameters, target book, trend overlay, tranche schedule, order plan |
| 5 | `quant.execution` | preflight, the engine (the only code that can send an order, and only with an owner token per plan), hash-chained audit log, tranche ledger, the Alpaca adapter |
| 6 | `quant.ops` | monitors |

Layers import downwards only (`tools/check_layout.py`); the production commands load only production code (`tools/check_imports.py`).
`r/` holds the R side (release verifier, statistics, evaluation metrics; `renv.lock`), and `rust_core/` the order planner, pre-trade checks and release verification in Rust with no third-party crates; `contracts/RELEASE_V1.md` is
the release format both languages verify. Architecture: [wiki](https://github.com/CustomBooch/quant-equity-system/wiki/Architecture).

## Standards

| Rule | Enforced by |
| --- | --- |
| No look-ahead: a value is used only after the date it became public | `quant.data.sources.base.validate`, `quant.features.sources`, [Point-in-time contracts](https://github.com/CustomBooch/quant-equity-system/wiki/Point-in-time-contracts) |
| A release is verified by hash before any file in it is read | `quant.core.release`, `rust_core` |
| No order without preflight GO, an owner token for that exact plan, and the Rust pre-trade check | `quant.execution.engine`, `rust_core` |
| Layering and production imports | `tools/check_layout.py`, `tools/check_imports.py` |
| Typed (`mypy --strict` on the production path), linted, no duplicated functions | `pyproject.toml`, `tools/check_duplicates.py` |

## Documentation

The [wiki](https://github.com/CustomBooch/quant-equity-system/wiki) holds the detail: architecture, adding a dataset, point-in-time contracts, the Rust core and the statistics reference.

## Disclaimer

Research software, provided as is under the MIT licence. Nothing here is investment advice. Data terms are yours to check: the licence table
(`tools/py -m quant licences`) records what each source may be used for.
