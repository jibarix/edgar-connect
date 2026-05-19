"""Industry-specific extension-tag → canonical concept mappings.

The Company Facts API filters company-extension XBRL concepts (anything
not us-gaap / dei / srt / etc.). For some industries the relevant line
items live exclusively in extensions — dealer floor-plan debt being the
canonical example, but the same pattern affects bank-specific D&A,
insurance-segment reserves, etc.

Each `ExtensionRule` is a regex applied to the concept's local name
(after stripping the namespace prefix). When a rule fires, the fact is
re-tagged under a synthetic taxonomy `ext:` with the canonical concept
name, then injected into the same normalization pipeline as us-gaap
facts. Multiple raw extension tags can collapse to the same canonical
concept — they're summed per period before injection.

Add new industries here as separate rule lists; the `parse_company_with_extensions`
caller selects which set to apply based on SIC / industry context.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class ExtensionRule(NamedTuple):
    """Maps an extension concept's local name to a canonical concept."""
    pattern: re.Pattern
    category: str           # "Liabilities", "OperatingCashFlow", etc.
    canonical: str          # synthetic concept name (no prefix)
    period_type: str        # "instant" or "duration" — must match fact context


# ── Dealers (SIC 5500-5599) ─────────────────────────────────────────
# Tags compiled from sample 10-Ks of ABG / AN / KMX / LAD / GPI.

DEALER_RULES: list[ExtensionRule] = [
    # ── Floor plan notes payable (balance) ──
    # ABG / LAD: FloorPlanNotesPayable[Trade|NonTrade]
    ExtensionRule(
        pattern=re.compile(r"^Floor[Pp]lanNotesPayable(?:Trade|NonTrade)?$"),
        category="Liabilities",
        canonical="FloorPlanNotesPayable",
        period_type="instant",
    ),
    # AN: VehicleFloorplanPayable (single combined balance — no Trade/NonTrade split)
    ExtensionRule(
        pattern=re.compile(r"^VehicleFloorplanPayable$"),
        category="Liabilities",
        canonical="FloorPlanNotesPayable",
        period_type="instant",
    ),
    # SAH: VehicleFloorPlanPayable[Trade|NonTrade] (CamelCase "FloorPlan",
    # no "Notes" mid-word — distinct from AN's spelling)
    ExtensionRule(
        pattern=re.compile(r"^VehicleFloorPlanPayable(?:Trade|NonTrade)?$"),
        category="Liabilities",
        canonical="FloorPlanNotesPayable",
        period_type="instant",
    ),
    # GPI: split as CreditFacilityGross + ManufacturerAffiliates
    ExtensionRule(
        pattern=re.compile(r"^FloorplanNotesPayable(?:CreditFacilityGross|ManufacturerAffiliates)$"),
        category="Liabilities",
        canonical="FloorPlanNotesPayable",
        period_type="instant",
    ),
    # NOTE: LAD also tags `lad:FloorPlanDebt`, but that's the SUM of their
    # Trade + NonTrade tags above — including it here would double-count.
    # The Trade/NonTrade split rule covers LAD correctly.

    # NOTE: KMX tags `kmx:NonRecourseNotesPayable` for the CAF asset-backed
    # notes (~$17B at FY26), BUT it also already rolls those notes into the
    # us-gaap `LongTermDebt` total ($16.6B at 2025-11-30 = $15.97B non-recourse
    # + ~$615M recourse). Re-injecting the extension causes a 2x double-count
    # of the non-recourse balance. Capture is therefore intentionally omitted —
    # the standard long_term_debt chain already covers it.

    # ── ABG loaner-vehicle financing ──
    ExtensionRule(
        pattern=re.compile(r"^NotesPayableLoanerVehicleCurrent$"),
        category="Liabilities",
        canonical="LoanerVehicleNotesPayable",
        period_type="instant",
    ),

    # ── Custom D&A on cash-flow statement ──
    # AN tags `DepreciationAndAmortizationExcludingDebtFinancingCostsAndDiscounts`
    # instead of any standard us-gaap D&A concept.
    ExtensionRule(
        pattern=re.compile(r"^DepreciationAndAmortizationExcludingDebtFinancingCostsAndDiscounts$"),
        category="OperatingCashFlow",
        canonical="DepreciationAndAmortization",
        period_type="duration",
    ),
    # GPI tags `DepreciationDepletionAndAmortizationContinuingOperations`.
    ExtensionRule(
        pattern=re.compile(r"^DepreciationDepletionAndAmortizationContinuingOperations$"),
        category="OperatingCashFlow",
        canonical="DepreciationAndAmortization",
        period_type="duration",
    ),
]


# ── Captive-finance equipment/vehicle OEMs ──────────────────────────
# Manufacturers with a consolidated captive lender (Deere SIC 3523,
# CNH, etc.). Post-FY2022 Deere stopped tagging the face long-term
# debt total under any us-gaap concept and moved it to the company
# extension `de:LongTermDebtAndFinanceLeasesNoncurrent` — which the
# Company Facts API strips, leaving the flat feed with only a stale
# `us-gaap:LongTermDebtNoncurrent` frozen at FY2022. Re-inject the
# extension under the canonical noncurrent-debt concept so the standard
# long_term_debt_noncurrent chain resolves it.
#
# Scope is deliberately narrow:
#   * The CURRENT maturities are tagged plain `us-gaap:DebtCurrent`
#     (a standard concept present in the flat feed) — handled by the
#     long_term_debt_current concept chain, NOT here.
#   * `us-gaap:SecuredDebt` (securitization borrowings) is a SUBSET
#     already inside the consolidated noncurrent total; capturing it
#     would double-count, so it is intentionally NOT matched.

EQUIPMENT_FINANCE_RULES: list[ExtensionRule] = [
    ExtensionRule(
        pattern=re.compile(r"^LongTermDebtAndFinanceLeasesNoncurrent$"),
        category="Liabilities",
        canonical="LongTermDebtNoncurrent",
        period_type="instant",
    ),
]


def apply_rules(
    facts: list[dict],
    rules: list[ExtensionRule],
) -> dict[tuple[str, str, str], dict]:
    """Apply extension rules to a fact list and aggregate per period.

    Returns a dict keyed by (canonical_concept, period_end, period_type)
    where the value is a synthetic fact dict carrying the SUMMED value
    across all matching raw extension facts for that period.

    Aggregation matters because dealers split floor plan into multiple
    sub-tags (Trade + NonTrade, or CreditFacilityGross + ManufacturerAffiliates)
    that must be summed for the canonical balance.
    """
    aggregated: dict[tuple[str, str, str], dict] = {}
    for fact in facts:
        for rule in rules:
            if rule.period_type != fact["period_type"]:
                continue
            if not rule.pattern.match(fact["concept"]):
                continue
            key = (rule.canonical, fact["period_end"], fact["period_type"])
            existing = aggregated.get(key)
            if existing is None:
                aggregated[key] = {
                    "category": rule.category,
                    "canonical": rule.canonical,
                    "value": fact["value"],
                    "period_start": fact["period_start"],
                    "period_end": fact["period_end"],
                    "period_type": fact["period_type"],
                    "unit": fact["unit"],
                    "source_concepts": [f"{fact['prefix']}:{fact['concept']}"],
                }
            else:
                existing["value"] += fact["value"]
                src = f"{fact['prefix']}:{fact['concept']}"
                if src not in existing["source_concepts"]:
                    existing["source_concepts"].append(src)
            break  # one rule per fact — first match wins
    return aggregated
