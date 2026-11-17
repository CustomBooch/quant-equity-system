# Release contract v1

A **release** is a directory of files handed from one language to another (Python writes; R and Rust read). It carries one manifest, `VERSION.json`, and every reader verifies it before using any file.

```json
{
  "schema": "release/1",
  "release": "frozen_check_v1",
  "description": "free text",
  "created_utc": "2026-10-21T20:00:00+00:00",
  "code_commit": "d6633ef...",
  "inputs": {"label": "sha256 of an upstream file this release was derived from"},
  "files": {"diff_series.csv": {"sha256": "...", "bytes": 12345}},
  "expected": {"anything": "the writer's own numbers, for readers to check against"},
  "validation": {"report_sha256": "sha256 of the validation report file in this release", "gates": {"G1_pit_parity": "pass", "G2_coverage": "pass"}}
}
```

Rules

1. `files` lists every data file in the release (relative paths, `/` separators), except `VERSION.json` itself. A reader verifies each listed file (size and SHA-256) and **refuses** a release in which a file is missing, changed, or present on disk but not listed.
2. `schema` must equal `release/1`; readers refuse anything else.
3. Releases are read-only to R and Rust. Derived from licensed data means private: not committed (the data files are git-ignored), not shared.
4. `validation` is optional for plain hand-offs and **required for data-layer releases** (a data vendor's output contract). A reader that asks for validation (`require_validation`) refuses a release with no `validation` block, a `report_sha256` that is not the hash of a listed file, no gates, or any gate whose value is not exactly `"pass"`.
5. `expected` is optional and is the writer's claim, not proof: a reader recomputes and compares.

Writer: `lab/release` (Python, `write_release`). Verifiers: `lab/release` (`verify_release`), `rust_core verify-release <dir>`, `r/R/release.R` (`verify_release`). All three agree on the same directories (tested).

## Data-layer releases

For `normalised/<table>/<version_id>/` the directory is a release whose `files` include the table (Parquet), the data dictionary, the coverage-by-year report, the missingness report and the validation report; `inputs` holds the SHA-256 of every raw input file; `code_commit` is the commit that built it; `validation` carries the report hash and the gate results. Any change creates a new version id: writers refuse to overwrite a manifest. R records the `release` id in every result it produces.
