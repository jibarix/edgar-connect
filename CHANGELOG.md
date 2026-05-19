# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-05-19

### Added

- **Hardened CI workflow (`.github/workflows/ci.yml`).** Triggers on
  `push`/`pull_request` to `main` only; no `pull_request_target`;
  least-privilege token (`permissions: contents: read`); third-party
  actions pinned to full commit SHAs. Install is hash-verified from the
  Windows/cp311 `requirements.lock` plus exact, supply-chain-audited dev
  pins (`--no-deps`) and an editable `--no-build-isolation --no-deps`
  install — no fresh dependency resolve, consistent with the active
  Mini Shai-Hulud policy. Runs `pytest` and an offline MCP server import
  boot; no live SEC calls (`EDGAR_IDENTITY` intentionally unset).
  Pinned to `windows-latest` / Python 3.11 to match the committed lock;
  a Linux runner would force an incident-blocked cross-platform resolve.

### Changed

- **`requires-python` corrected to `>=3.11`.** The pinned dependency
  set (`pandas==3.0.2`, `numpy==2.4.4`) requires Python 3.11+, so the
  package cannot install on a lower interpreter. Published metadata now
  matches that reality; README updated to `Python 3.11+` and the prior
  "source-compatible vs lock-verified" ambiguity removed. Lowering the
  floor again would require a separately generated and audited
  3.10-compatible dependency set with CI validation first.

## [0.1.0] - 2026-05-19

### Fixed

- **CLI `--cik` fallback path.** A CIK-only request set `ticker = "UNKNOWN"`,
  and the HTML/XML fallback resolved filings purely from ticker, so it could
  never succeed for CIK-only usage. `StatementExtractor` now threads an
  optional `cik` through `extract_statement` /
  `get_statement_soup` / `get_statement_file_names_in_filing_summary` via a
  new `_resolve_cik` helper that prefers an explicit CIK over ticker lookup.
- **Fallback statement metadata.** `format_statement_data()` derived
  `period_type` from `statement_type` (`"annual" if statement_type ==
  "annual" else "quarterly"`), a check that could never be true, so the
  non-`ALL` path always reported `"quarterly"` and the `ALL` path hardcoded
  `"annual"`. The real `period_type` is now threaded through both branches.
- **Interactive CLI crash on bad input.** Company-selection and
  number-of-periods prompts called `int(input(...))` unguarded; a
  non-numeric keystroke raised an uncaught `ValueError` and aborted the
  session. A new `prompt_int()` helper re-prompts instead.

### Changed

- **Smoke test now fails loudly.** `scripts/smoke_test_metrics.py` previously
  printed `Periods loaded: []`, logged connection errors, and still exited
  `0` when live SEC fetches failed. It now exits non-zero on empty Company
  Facts, no parsed periods, or all-`None` metrics, so a failed live run is a
  real failure signal.
- **Pinned dev tooling for supply-chain safety.** Replaced the unpinned
  `pytest>=8.0` dev extra with `pytest==8.4.2` plus an explicit
  `packaging==25.0` transitive override, consistent with the runtime pin
  policy (versions verified uploaded before the active incident window).

### Added

- **Offline regression test suite (`tests/`, 18 tests).** No live SEC calls;
  covers the `--cik` resolver precedence and signature contract,
  `period_type` threading, `prompt_int` re-prompt behavior, and the derived
  metric registry surface.

### Validation

- Live SEC validation performed against Apple Inc. (CIK 0000320193): the
  fail-loud smoke test passed (exit 0, hand-verified FY2025 figures) and a
  live CIK-only `--cik` CLI run produced a correct multi-period income
  statement. The CIK-only run exercised the primary XBRL route; the
  HTML/XML fallback remains covered by unit tests rather than a live run.
