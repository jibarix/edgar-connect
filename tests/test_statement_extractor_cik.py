"""Regression tests for the ``--cik`` fallback path.

Before the fix, a CIK-only CLI request set ``ticker = "UNKNOWN"`` and the
HTML/XML fallback resolved filings purely from ticker
(``cik_matching_ticker``), so it could never resolve a CIK-only request.
``_resolve_cik`` must prefer an explicitly supplied CIK and skip the
network ticker lookup entirely. These tests never hit the network:
``cik_matching_ticker`` is stubbed to assert it is not called when a CIK
is supplied.
"""
import pytest

from edgar.statement_extractor import StatementExtractor


def test_explicit_cik_is_zero_padded_and_skips_ticker_lookup():
    ext = StatementExtractor()

    def _boom(_ticker):
        raise AssertionError(
            "cik_matching_ticker must not be called when a CIK is supplied"
        )

    ext.cik_matching_ticker = _boom
    assert ext._resolve_cik("UNKNOWN", "320193") == "0000320193"


def test_already_padded_cik_is_stable():
    ext = StatementExtractor()
    ext.cik_matching_ticker = lambda _t: pytest.fail("should not be called")
    assert ext._resolve_cik("UNKNOWN", "0000320193") == "0000320193"


def test_no_cik_falls_back_to_ticker_resolution():
    ext = StatementExtractor()
    ext.cik_matching_ticker = lambda ticker: f"resolved::{ticker}"
    assert ext._resolve_cik("AAPL", None) == "resolved::AAPL"


def test_extract_statement_accepts_cik_kwarg():
    # The CLI fallback calls extract_statement(..., cik=params["cik"]);
    # guard the signature so that wiring can't silently regress.
    import inspect

    sig = inspect.signature(StatementExtractor.extract_statement)
    assert "cik" in sig.parameters
    assert sig.parameters["cik"].default is None
