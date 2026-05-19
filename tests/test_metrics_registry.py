"""Offline guardrails for the derived-metric registry.

These don't touch the SEC. They assert the public metric surface loads
and a stable set of headline slugs stays registered with the expected
spec shape (``fn`` callable, ``unit`` string), so an import-time or
decorator regression fails in CI rather than only in the live smoke test.
"""
import edgar.metrics as edgar_metrics

# Headline slugs that downstream consumers (CLI, MCP, smoke test) rely on.
CORE_SLUGS = [
    "revenue",
    "gross_profit",
    "ebit",
    "ebitda",
    "ni",
    "fcf",
    "current_ratio",
    "debt_to_equity",
    "roe",
    "roic",
]


def test_registry_loads_and_is_nonempty():
    assert len(edgar_metrics.REGISTRY) > 0


def test_core_slugs_are_registered():
    missing = [s for s in CORE_SLUGS if s not in edgar_metrics.REGISTRY]
    assert not missing, f"missing core metrics: {missing}"


def test_specs_have_callable_fn_and_unit():
    for slug in CORE_SLUGS:
        spec = edgar_metrics.REGISTRY[slug]
        assert callable(spec.fn), f"{slug}.fn is not callable"
        assert isinstance(spec.unit, str) and spec.unit, f"{slug}.unit invalid"


def test_normalized_statement_is_exported():
    assert hasattr(edgar_metrics, "NormalizedStatement")
