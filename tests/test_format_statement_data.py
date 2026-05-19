"""Regression tests for ``main.format_statement_data`` metadata.

Before the fix, the non-``ALL`` branch set ``period_type`` via
``"annual" if statement_type == "annual" else "quarterly"``. Since
``statement_type`` is always BS/IS/CF/ALL, that branch could never
report annual. The real ``period_type`` must now be threaded through.
"""
import pandas as pd

import main


def _bs_frame():
    return pd.DataFrame({"Assets": [100.0], "Liabilities": [40.0]})


def test_non_all_threads_quarterly():
    data = {"2025-09-30": _bs_frame()}
    out = main.format_statement_data(data, "BS", "quarterly")
    assert out["metadata"]["period_type"] == "quarterly"


def test_non_all_threads_annual():
    data = {"2025-09-30": _bs_frame()}
    out = main.format_statement_data(data, "BS", "annual")
    assert out["metadata"]["period_type"] == "annual"


def test_non_all_default_is_annual():
    data = {"2025-09-30": _bs_frame()}
    out = main.format_statement_data(data, "BS")
    assert out["metadata"]["period_type"] == "annual"


def test_all_branch_threads_period_type():
    data = {"BS": {"2025-09-30": _bs_frame()}}
    out = main.format_statement_data(data, "ALL", "quarterly")
    assert out["metadata"]["period_type"] == "quarterly"


def test_metrics_are_namespaced_by_statement_type():
    data = {"2025-09-30": _bs_frame()}
    out = main.format_statement_data(data, "BS", "annual")
    assert "BS_Assets" in out["metrics"]
    assert out["metrics"]["BS_Assets"]["values"]["2025-09-30"] == 100.0


def test_empty_input_returns_none():
    assert main.format_statement_data({}, "BS", "annual") is None
