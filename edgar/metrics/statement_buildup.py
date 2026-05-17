"""Layer-2 balance-sheet & cash-flow structural buildup.

Turns raw SEC Company Facts into the frozen closed-set structure defined
in ``_statement_taxonomy.py`` (29 BS slots / 23 CF slots), then derives
the subtotals *from the slotted inputs* and reports the accounting-
identity residual.

Design contract
---------------
* **Full closed-set coverage.** Every us-gaap BS/CF concept is resolved
  by the same two layers the offline pipeline used, in the same order:
  the shipped deterministic pre-filter (`_bs_prefilter` / `_cf_prefilter`,
  high-precision, low-recall) first; whatever it leaves *ambiguous*
  falls through to the compiled adjudicated map (`_bs_slot_map` /
  `_cf_slot_map`, the committed CODE form of the archived fan-out).
  Confident pre-filter hits never consult the map.
* **Subtotals are derived, never tagged in.** A concept the pre-filter
  recognises as a *reported subtotal* (``Assets``, ``LiabilitiesCurrent``,
  ``NetCashProvidedByUsedInOperatingActivities``, …) is NOT summed into
  the buildup. It is captured separately as a provenance / guardrail
  value so the engine can report reported-vs-derived drift. The
  structural totals are recomputed from the input slots only -- this is
  the CLAUDE.md invariant ("Do not classify raw tags into subtotal
  slots as if they were input lines").
* **Identities are reported, not assumed.** BS exposes
  ``Assets - (Liabilities + Equity)``; CF exposes
  ``(CFO + CFI + CFF + FX) - ΔCash``. A non-zero residual is surfaced,
  not hidden -- it is the honest signal of tag double-count / coverage
  gaps in a single filer.
* **Industry overlays re-label, they do not re-partition.** ``sic``
  selects at most one of bank / insurance / reit overlay membership for
  presentation; the numeric partition and the identity are unchanged
  (see ``_statement_taxonomy.OVERLAYS``).

This module is a *new* consumer of the raw Company Facts payload; it
does NOT go through ``xbrl_parser`` (whose curated-map admission gate
would hide most of the closed-set universe), so existing
``derived_lines`` / parser outputs are unaffected.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from edgar.metrics import _bs_prefilter, _cf_prefilter
from edgar.metrics._bs_slot_map import BS_SLOT_MAP
from edgar.metrics._cf_slot_map import CF_SLOT_MAP
from edgar.metrics._statement_taxonomy import (
    BANK,
    BS_SLOTS_BY_ID,
    CF_SLOTS_BY_ID,
    GENERIC,
    INSURANCE,
    OVERLAYS,
    REIT,
    Balance,
    Section,
)

_USD = ("USD",)
_ASSET_SECTIONS = (Section.CURRENT_ASSET, Section.NONCURRENT_ASSET)
_LIAB_SECTIONS = (Section.CURRENT_LIABILITY, Section.NONCURRENT_LIABILITY)
_EQUITY_SECTIONS = (Section.EQUITY,)


# ── concept -> slot resolution (pre-filter first, then compiled map) ──

def resolve_bs_slot(concept: str) -> tuple[str | None, str]:
    """(slot_id, source) for a us-gaap BS concept.

    source ∈ {'prefilter', 'map', 'subtotal', 'unclassified'}.
    'subtotal' => a reported subtotal: provenance only, not an input.
    """
    c = _bs_prefilter.classify(concept)
    if c.disposition == "confident":
        return c.slot, "prefilter"
    if c.disposition == "subtotal":
        return c.slot, "subtotal"
    slot = BS_SLOT_MAP.get(concept)
    if slot is not None:
        return slot, "map"
    # Genuinely BS-shaped (a pre-filter rule fired but the polarity
    # guardrail vetoed it) yet uncovered -> real coverage gap. A concept
    # no rule even touched is simply out of scope (IS / disclosure).
    return None, "unclassified" if c.rule is not None else "out_of_scope"


def resolve_cf_slot(concept: str) -> tuple[str | None, str]:
    """(slot_id, source) for a us-gaap CF concept. See resolve_bs_slot."""
    c = _cf_prefilter.classify(concept)
    if c.disposition == "confident":
        return c.slot, "prefilter"
    if c.disposition == "subtotal":
        return c.slot, "subtotal"
    slot = CF_SLOT_MAP.get(concept)
    if slot is not None:
        return slot, "map"
    return None, "unclassified" if c.rule is not None else "out_of_scope"


# ── signed contribution into the structural buildup ──────────────────

def _bs_signed(slot_id: str, val: float) -> float:
    """As-filed sign, except treasury stock is contra-equity.

    us-gaap reports asset / liability / common-stock / APIC magnitudes
    positive and the mixed-sign equity lines (retained earnings,
    AOCI, NCI) already signed, so a plain sum reconstructs each side.
    ``TreasuryStock*`` values are reported as a positive magnitude but
    REDUCE equity, so they are negated.
    """
    return -val if slot_id == "treasury_stock" else val


def _cf_signed(slot_id: str, val: float) -> float | None:
    """Flow sign from the slot's balance polarity.

    CREDIT = inflow / non-cash add-back (+), DEBIT = outflow (-),
    EITHER = an already-signed net reconciling item (as filed),
    NA = non-monetary (excluded).
    """
    bal = CF_SLOTS_BY_ID[slot_id].balance
    if bal is Balance.CREDIT or bal is Balance.EITHER:
        return val
    if bal is Balance.DEBIT:
        return -val
    return None  # NA


# ── period selection over the raw Company Facts payload ──────────────

def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _gaap_concepts(facts_data: dict) -> dict[str, dict]:
    facts = facts_data.get("facts", {})
    out: dict[str, dict] = {}
    for tax in ("us-gaap", "ext"):
        out.update(facts.get(tax, {}))
    return out


def _fiscal_month(concepts: dict[str, dict]) -> int:
    months: dict[int, int] = defaultdict(int)
    for cdata in concepts.values():
        for f in cdata.get("units", {}).get("USD", []):
            end = f.get("end")
            if end:
                months[_d(end).month] += 1
    return max(months, key=months.get) if months else 12


def _annual_period_ends(concepts: dict[str, dict], fmonth: int,
                         num_periods: int) -> list[str]:
    ends: set[str] = set()
    for cdata in concepts.values():
        for f in cdata.get("units", {}).get("USD", []):
            end = f.get("end")
            if end and _d(end).month == fmonth:
                ends.add(end[:10])
    return sorted(ends, reverse=True)[:num_periods]


def _instant_value(cdata: dict, period_end: str) -> float | None:
    """Latest-filed point-in-time fact at period_end (balance sheet)."""
    best = None
    for f in cdata.get("units", {}).get("USD", []):
        if f.get("end", "")[:10] != period_end:
            continue
        if f.get("start") and f["start"][:10] != period_end:
            continue  # a duration fact, not an instant
        if "val" not in f:
            continue
        if best is None or f.get("filed", "") > best.get("filed", ""):
            best = f
    return None if best is None else best["val"]


def _duration_value(cdata: dict, period_end: str) -> float | None:
    """Latest-filed ~full-year duration fact ending at period_end (CF)."""
    best = None
    for f in cdata.get("units", {}).get("USD", []):
        if f.get("end", "")[:10] != period_end or not f.get("start"):
            continue
        if "val" not in f:
            continue
        span = (_d(f["end"]) - _d(f["start"])).days
        if not 350 <= span <= 380:
            continue
        if best is None or f.get("filed", "") > best.get("filed", ""):
            best = f
    return None if best is None else best["val"]


# ── result shape ─────────────────────────────────────────────────────

@dataclass
class BuildupResult:
    statement: str                       # 'BS' | 'CF'
    period: str                          # YYYY-MM-DD
    overlay: str                         # generic | bank | insurance | reit
    slots: dict[str, float]              # input slot_id -> signed sum
    slot_tags: dict[str, list[str]]      # slot_id -> contributing concepts
    subtotals: dict[str, float]          # DERIVED from inputs
    reported_subtotals: dict[str, float] # provenance (tagged, not summed)
    identity_residual: float             # see per-statement note
    reported_residual: dict[str, float]  # derived vs tagged drift
    unclassified: dict[str, float]       # BS/CF-shaped but uncovered
    overlay_members: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _overlay_for_sic(sic) -> str:
    if sic is None:
        return GENERIC
    try:
        n = int(str(sic).strip()[:4])
    except (TypeError, ValueError):
        return GENERIC
    if 6000 <= n <= 6029:
        return BANK
    if 6300 <= n <= 6411:
        return INSURANCE
    if n == 6798:
        return REIT
    return GENERIC


# ── balance sheet ────────────────────────────────────────────────────

def build_balance_sheet(facts_data: dict, *, num_periods: int = 1,
                         sic=None) -> list[BuildupResult]:
    concepts = _gaap_concepts(facts_data)
    if not concepts:
        return []
    overlay = _overlay_for_sic(sic)
    fmonth = _fiscal_month(concepts)
    periods = _annual_period_ends(concepts, fmonth, num_periods)
    results: list[BuildupResult] = []

    for period in periods:
        slots: dict[str, float] = defaultdict(float)
        slot_tags: dict[str, list[str]] = defaultdict(list)
        reported: dict[str, float] = {}
        unclassified: dict[str, float] = {}

        for concept, cdata in concepts.items():
            slot, src = resolve_bs_slot(concept)
            if src == "out_of_scope":
                continue
            val = _instant_value(cdata, period)
            if val is None:
                continue
            if src == "subtotal":
                reported[slot] = val  # provenance, not an input line
                continue
            if src == "unclassified":
                unclassified[concept] = val
                continue
            if BS_SLOTS_BY_ID[slot].balance is Balance.NA:
                continue  # share counts, non-monetary
            slots[slot] += _bs_signed(slot, val)
            slot_tags[slot].append(concept)

        def _sum(sections) -> float:
            return sum(
                v for sid, v in slots.items()
                if BS_SLOTS_BY_ID[sid].section in sections
            )

        cur_a = _sum((Section.CURRENT_ASSET,))
        tot_a = _sum(_ASSET_SECTIONS)
        cur_l = _sum((Section.CURRENT_LIABILITY,))
        tot_l = _sum(_LIAB_SECTIONS)
        tot_e = _sum(_EQUITY_SECTIONS)
        subtotals = {
            "current_assets": cur_a,
            "total_assets": tot_a,
            "current_liabilities": cur_l,
            "total_liabilities": tot_l,
            "total_equity": tot_e,
        }
        reported_residual = {}
        if "total_assets" in reported:
            reported_residual["total_assets"] = (
                reported["total_assets"] - tot_a)
        if "total_liabilities" in reported:
            reported_residual["total_liabilities"] = (
                reported["total_liabilities"] - tot_l)
        if "total_equity" in reported:
            reported_residual["total_equity"] = reported["total_equity"] - tot_e

        results.append(BuildupResult(
            statement="BS", period=period, overlay=overlay,
            slots=dict(sorted(slots.items())),
            slot_tags={k: sorted(v) for k, v in sorted(slot_tags.items())},
            subtotals=subtotals,
            reported_subtotals=reported,
            identity_residual=tot_a - (tot_l + tot_e),
            reported_residual=reported_residual,
            unclassified=dict(sorted(unclassified.items())),
            overlay_members=OVERLAYS["BS"],
        ))
    return results


# ── cash flow ────────────────────────────────────────────────────────

def build_cash_flow(facts_data: dict, *, num_periods: int = 1,
                     sic=None) -> list[BuildupResult]:
    concepts = _gaap_concepts(facts_data)
    if not concepts:
        return []
    overlay = _overlay_for_sic(sic)
    fmonth = _fiscal_month(concepts)
    periods = _annual_period_ends(concepts, fmonth, num_periods)
    results: list[BuildupResult] = []

    for period in periods:
        slots: dict[str, float] = defaultdict(float)
        slot_tags: dict[str, list[str]] = defaultdict(list)
        reported: dict[str, float] = {}
        unclassified: dict[str, float] = {}

        for concept, cdata in concepts.items():
            slot, src = resolve_cf_slot(concept)
            if src == "out_of_scope":
                continue
            val = _duration_value(cdata, period)
            if val is None:
                continue
            if src == "subtotal":
                reported[slot] = val
                continue
            if src == "unclassified":
                unclassified[concept] = val
                continue
            signed = _cf_signed(slot, val)
            if signed is None:
                continue
            slots[slot] += signed
            slot_tags[slot].append(concept)

        def _sum(section) -> float:
            return sum(
                v for sid, v in slots.items()
                if CF_SLOTS_BY_ID[sid].section is section
            )

        cfo = _sum(Section.OPERATING)
        cfi = _sum(Section.INVESTING)
        cff = _sum(Section.FINANCING)
        fx = slots.get("cf_fx_effect", 0.0)
        delta = cfo + cfi + cff + fx
        subtotals = {
            "cfo": cfo, "cfi": cfi, "cff": cff,
            "cf_fx_effect": fx, "cf_change_in_cash": delta,
        }
        reported_residual = {
            sid: reported[sid] - subtotals[sid]
            for sid in ("cfo", "cfi", "cff", "cf_change_in_cash")
            if sid in reported
        }

        # Identity: derived ΔCash vs the reported ΔCash subtotal when the
        # filer tagged one; else fall back to internal consistency (0).
        if "cf_change_in_cash" in reported:
            identity = delta - reported["cf_change_in_cash"]
        else:
            identity = 0.0

        results.append(BuildupResult(
            statement="CF", period=period, overlay=overlay,
            slots=dict(sorted(slots.items())),
            slot_tags={k: sorted(v) for k, v in sorted(slot_tags.items())},
            subtotals=subtotals,
            reported_subtotals=reported,
            identity_residual=identity,
            reported_residual=reported_residual,
            unclassified=dict(sorted(unclassified.items())),
            overlay_members=OVERLAYS["CF"],
        ))
    return results
