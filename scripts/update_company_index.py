"""Update system for ``data/company_index.json``.

The file is the local company classification index (~6,800 companies)
that ``edgar/company_classifier.py`` produces from SEC Financial
Statement Data Set quarters: ``{cik: {name, sic, industry, subindustry,
country_inc, state_inc, revenue_country, revenue_pct, period,
geo_breakdown}}``. It's the backing data for the MCP
``search_companies`` tool.

Design contract (companion to ``update_sec_tag_mapping.py``; documented
for the next maintainer):

* **Snapshot, not additive.** The index is "the latest annual filing per
  CIK across the N supplied FSDS quarters." A rebuild fully replaces it.
  This is unlike the tag mapping, which appends.
* **Forward-only integrity.** A manifest at
  ``data/company_index.source.json`` records the current file's sha256,
  which FSDS quarters fed it, and per-quarter zip shas. ``check`` and
  ``rebuild`` refuse to operate on a hand-edited index file (rc=2).
* **The classifier is reused, not reimplemented.** This script wraps
  ``edgar.company_classifier.build_index`` after staging the requested
  FSDS quarters into a temporary scratch dir, so the index built by this
  tool is byte-equal to the index a manual ``python -m
  edgar.company_classifier --build`` would produce on the same inputs.
* **Supply-chain hygiene.** stdlib-only. The live download path requires
  ``EDGAR_IDENTITY``; ``--source-zip`` lets offline / CI runs supply a
  local FSDS zip per quarter.

Usage::

    python scripts/update_company_index.py init
    python scripts/update_company_index.py check
    python scripts/update_company_index.py rebuild 2025q4 2026q1
    python scripts/update_company_index.py rebuild 2025q4 2026q1 --apply
    python scripts/update_company_index.py rebuild 2025q4 2026q1 \\
        --source-zip 2025q4=/tmp/2025q4.zip --source-zip 2026q1=/tmp/2026q1.zip \\
        --apply

The ``rebuild`` subcommand runs the full pipeline: integrity check ->
fetch each quarter (or read local zip) -> stage into a scratch dir ->
``build_index`` -> diff -> report. With ``--apply`` it also writes the
new index file and rotates the manifest.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

logger = logging.getLogger("update_company_index")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make ``edgar.company_classifier`` importable when the script is run
# from anywhere (e.g. ``python scripts/update_company_index.py ...``).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

INDEX_PATH = REPO_ROOT / "data" / "company_index.json"
MANIFEST_PATH = REPO_ROOT / "data" / "company_index.source.json"
FSDS_CACHE_DIR = REPO_ROOT / "data" / "sec_datasets"

# Same FSDS URL pattern used by update_sec_tag_mapping.py.
FSDS_URL_FMT = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{quarter}.zip"
QUARTER_RE = re.compile(r"^(20\d{2})q([1-4])$")


# ── Hashing & manifest ────────────────────────────────────────────────

def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest() -> dict | None:
    if not MANIFEST_PATH.is_file():
        return None
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_manifest(m: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
        f.write("\n")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── FSDS retrieval ────────────────────────────────────────────────────

def _validate_quarter(quarter: str) -> None:
    if not QUARTER_RE.match(quarter):
        raise SystemExit(f"Invalid quarter '{quarter}'. Expected form YYYYqN (e.g. 2026q1).")


def _identity_or_die() -> str:
    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not identity:
        raise SystemExit(
            "EDGAR_IDENTITY is not set. SEC fair-access policy requires every "
            "requester to identify themselves. Set it to 'Your Name your@email.com' "
            "before any live download, or pass --source-zip QUARTER=PATH for each "
            "quarter to use local FSDS zips."
        )
    return identity


def _download_fsds(quarter: str, dest: Path) -> None:
    """Fetch the FSDS zip for *quarter* into *dest*. Live SEC pull."""
    identity = _identity_or_die()
    url = FSDS_URL_FMT.format(quarter=quarter)
    logger.info("Downloading %s -> %s", url, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": identity})
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 (https)
        if resp.status != 200:
            raise SystemExit(f"SEC returned HTTP {resp.status} for {url}")
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    logger.info("Wrote %d bytes", dest.stat().st_size)


def _resolve_zips(
    quarters: list[str], source_zip_map: dict[str, str],
) -> dict[str, Path]:
    """Return ``{quarter: zip_path}`` for every requested quarter.

    Honors ``--source-zip QUARTER=PATH`` first; falls back to cached zip
    at ``data/sec_datasets/<quarter>.zip``; finally downloads.
    """
    resolved: dict[str, Path] = {}
    for q in quarters:
        if q in source_zip_map:
            p = Path(source_zip_map[q]).resolve()
            if not p.is_file():
                raise SystemExit(f"--source-zip {q}={p} not found")
            resolved[q] = p
            continue
        cached = FSDS_CACHE_DIR / f"{q}.zip"
        if cached.is_file():
            resolved[q] = cached
            continue
        # Need to download.
        _download_fsds(q, cached)
        resolved[q] = cached
    return resolved


def _extract_fsds(zip_path: Path, dest_dir: Path) -> None:
    """Extract the two files we need (``sub.txt``, ``num.txt``) into *dest_dir*.

    The full FSDS zip is several hundred MB; we only need two members,
    so partial extraction keeps the scratch dir small.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = {n.lower(): n for n in zf.namelist()}
        for want in ("sub.txt", "num.txt"):
            name = names.get(want)
            if name is None:
                raise SystemExit(f"FSDS zip {zip_path} is missing {want}")
            with zf.open(name) as src, open(dest_dir / want, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)


# ── Build via the existing classifier ─────────────────────────────────

def _build_index_from_scratch_dir(scratch: Path, quarters: list[str]) -> dict:
    """Run ``company_classifier.build_index`` against *scratch* as DATA_DIR.

    The classifier's module-level ``DATA_DIR`` is monkey-patched for the
    duration of the call so the index is built from exactly the supplied
    quarters and nothing else (no leak from the real cache).
    """
    from edgar import company_classifier as cc

    saved = cc.DATA_DIR
    cc.DATA_DIR = str(scratch)
    try:
        return cc.build_index(max_quarters=len(quarters))
    finally:
        cc.DATA_DIR = saved


# ── Diff ──────────────────────────────────────────────────────────────

def _diff(old: dict[str, dict], new: dict[str, dict]) -> dict:
    """Snapshot diff: added CIKs, removed CIKs, changed CIKs.

    ``changed`` is split into ``changed_period_only`` (the only diff is
    ``period`` moving forward — expected, low signal) and
    ``changed_substantive`` (any other field differs — worth surfacing).
    """
    added = [c for c in new if c not in old]
    removed = [c for c in old if c not in new]
    changed_period_only: list[str] = []
    changed_substantive: list[tuple[str, list[str]]] = []
    for cik, entry in new.items():
        prev = old.get(cik)
        if prev is None:
            continue
        diffs = [k for k in set(entry) | set(prev) if entry.get(k) != prev.get(k)]
        if not diffs:
            continue
        if diffs == ["period"]:
            changed_period_only.append(cik)
        else:
            changed_substantive.append((cik, sorted(diffs)))
    return {
        "added": added,
        "removed": removed,
        "changed_period_only": changed_period_only,
        "changed_substantive": changed_substantive,
    }


# ── Serialization (deterministic) ─────────────────────────────────────

def _dump_index(index: dict[str, dict]) -> bytes:
    """Serialize deterministically: sort by CIK, fixed key order per entry."""
    keys = ("name", "sic", "industry", "subindustry", "country_inc",
            "state_inc", "revenue_country", "revenue_pct", "period",
            "geo_breakdown")
    ordered = {}
    for cik in sorted(index.keys(), key=lambda c: int(c) if c.isdigit() else c):
        e = index[cik]
        ordered[cik] = {k: e.get(k) for k in keys}
    return (json.dumps(ordered, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# ── Subcommands ───────────────────────────────────────────────────────

def cmd_init(args: argparse.Namespace) -> int:
    """Bootstrap the manifest from the current index file."""
    if not INDEX_PATH.is_file():
        raise SystemExit(f"{INDEX_PATH} not found")

    if MANIFEST_PATH.is_file() and not args.force:
        existing = _load_manifest()
        print(f"Manifest already exists at {MANIFEST_PATH}.")
        print(f"  current_sha256:      {existing.get('current_sha256')}")
        print(f"  built_from_quarters: {existing.get('built_from_quarters')}")
        print("Pass --force to overwrite.")
        return 1

    current_sha = _sha256_file(INDEX_PATH)
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        idx = json.load(f)
    with_rc = sum(1 for v in idx.values() if v.get("revenue_country"))

    manifest = {
        "schema_version": 1,
        "current_sha256": current_sha,
        "built_from_quarters": None,
        "fsds_shas": {},
        "built_at": None,
        "companies_count": len(idx),
        "with_revenue_country_count": with_rc,
        "updated_at": _now_iso(),
        "notes": (
            "Forward-only integrity baseline. The current company_index.json "
            "predates this update system; its contents are accepted as "
            "historical ground truth and its source FSDS quarters are not "
            "recorded. From this manifest forward, every rebuild records its "
            "inputs (built_from_quarters + fsds_shas) and 'check' detects "
            "hand-edits."
        ),
        "history": [],
    }
    _save_manifest(manifest)
    print(f"Wrote {MANIFEST_PATH}")
    print(f"  current_sha256: {current_sha}")
    print(f"  companies:      {len(idx)} ({with_rc} with revenue_country)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Verify the current file's sha256 against the manifest."""
    manifest = _load_manifest()
    if manifest is None:
        raise SystemExit(f"No manifest at {MANIFEST_PATH}. Run 'init' first.")
    if not INDEX_PATH.is_file():
        raise SystemExit(f"{INDEX_PATH} not found")

    expected = manifest.get("current_sha256")
    actual = _sha256_file(INDEX_PATH)
    if actual != expected:
        print("INTEGRITY FAIL")
        print(f"  expected: {expected}")
        print(f"  actual:   {actual}")
        print(f"  {INDEX_PATH} has been modified outside the update system.")
        return 2
    print(f"OK  sha256={actual}")
    print(f"  built_from_quarters: {manifest.get('built_from_quarters')}")
    print(f"  built_at:            {manifest.get('built_at')}")
    print(f"  companies:           {manifest.get('companies_count')}")
    print(f"  with_revenue_country: {manifest.get('with_revenue_country_count')}")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Pull FSDS quarters, run the classifier, diff, optionally apply."""
    quarters = list(args.quarters)
    if not quarters:
        raise SystemExit("rebuild requires at least one quarter (e.g. 2026q1).")
    for q in quarters:
        _validate_quarter(q)

    source_zip_map: dict[str, str] = {}
    for spec in args.source_zip or []:
        if "=" not in spec:
            raise SystemExit(f"--source-zip must be QUARTER=PATH (got '{spec}')")
        q, p = spec.split("=", 1)
        if q not in quarters:
            raise SystemExit(f"--source-zip {q}=... but '{q}' is not in the quarters list")
        source_zip_map[q] = p

    manifest = _load_manifest()
    if manifest is None:
        raise SystemExit(f"No manifest at {MANIFEST_PATH}. Run 'init' first.")

    # 1) Integrity check.
    actual = _sha256_file(INDEX_PATH)
    if actual != manifest.get("current_sha256"):
        print("INTEGRITY FAIL -- refusing to rebuild.")
        print(f"  expected: {manifest.get('current_sha256')}")
        print(f"  actual:   {actual}")
        print(f"  {INDEX_PATH} has been modified outside the update system.")
        return 2

    # 2) Resolve each requested quarter to a local zip (cached or download).
    zips = _resolve_zips(quarters, source_zip_map)
    fsds_shas = {q: _sha256_file(p) for q, p in zips.items()}
    for q in quarters:
        print(f"FSDS {q}: {zips[q]}  sha256={fsds_shas[q]}")

    # 3) Stage every quarter's sub.txt + num.txt into a temp scratch dir.
    with tempfile.TemporaryDirectory(prefix="company_index_") as scratch_str:
        scratch = Path(scratch_str)
        logger.info("Scratch dir: %s", scratch)
        for q in quarters:
            _extract_fsds(zips[q], scratch / q)

        # 4) Run the classifier.
        logger.info("Building index from %d quarter(s) ...", len(quarters))
        new_index = _build_index_from_scratch_dir(scratch, quarters)

    if not new_index:
        print("Classifier returned an empty index. Aborting.")
        return 3

    # 5) Diff against current.
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        old_index = json.load(f)
    diff = _diff(old_index, new_index)

    new_count = len(new_index)
    new_with_rc = sum(1 for v in new_index.values() if v.get("revenue_country"))
    print()
    print(f"Diff against current ({len(old_index)} companies):")
    print(f"  + added CIKs:              {len(diff['added'])}")
    print(f"  - removed CIKs:            {len(diff['removed'])}")
    print(f"  ~ changed (period only):   {len(diff['changed_period_only'])}")
    print(f"  ~ changed (substantive):   {len(diff['changed_substantive'])}")
    print(f"  total in new index:        {new_count}")
    print(f"  with revenue_country:      {new_with_rc}  (was {sum(1 for v in old_index.values() if v.get('revenue_country'))})")

    def _sample(label: str, items: list, n: int = 10) -> None:
        if not items:
            return
        print(f"\nFirst {min(n, len(items))} {label}:")
        for c in items[:n]:
            if isinstance(c, tuple):
                cik, fields = c
                e = new_index.get(cik, {})
                print(f"  ~ CIK {cik:>10}  {e.get('name','')[:40]:<40}  fields={fields}")
            else:
                e = new_index.get(c) or old_index.get(c, {})
                print(f"    CIK {c:>10}  {e.get('name','')[:40]:<40}  SIC={e.get('sic','')}")
        if len(items) > n:
            print(f"  ... +{len(items) - n} more")

    _sample("added", diff["added"])
    _sample("removed", diff["removed"])
    _sample("substantive changes", diff["changed_substantive"])

    if args.report:
        rpath = Path(args.report).resolve()
        rpath.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "quarters": quarters,
            "fsds_shas": fsds_shas,
            "added": diff["added"],
            "removed": diff["removed"],
            "changed_period_only": diff["changed_period_only"],
            "changed_substantive": [
                {"cik": c, "fields": fs} for c, fs in diff["changed_substantive"]
            ],
            "companies_count": new_count,
            "with_revenue_country_count": new_with_rc,
        }
        with open(rpath, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\nWrote review report -> {rpath}")

    if not args.apply:
        print("\nDry-run (no --apply). Index NOT modified.")
        return 0

    # 6) Apply: write index deterministically, update manifest.
    body = _dump_index(new_index)
    new_sha = _sha256_bytes(body)
    with open(INDEX_PATH, "wb") as f:
        f.write(body)

    history = list(manifest.get("history") or [])
    history.append({
        "from_sha256": manifest.get("current_sha256"),
        "to_sha256": new_sha,
        "built_from_quarters": quarters,
        "fsds_shas": fsds_shas,
        "companies_count": new_count,
        "with_revenue_country_count": new_with_rc,
        "added_count": len(diff["added"]),
        "removed_count": len(diff["removed"]),
        "changed_period_only_count": len(diff["changed_period_only"]),
        "changed_substantive_count": len(diff["changed_substantive"]),
        "applied_at": _now_iso(),
    })
    manifest.update({
        "current_sha256": new_sha,
        "built_from_quarters": quarters,
        "fsds_shas": fsds_shas,
        "built_at": _now_iso(),
        "companies_count": new_count,
        "with_revenue_country_count": new_with_rc,
        "updated_at": _now_iso(),
        "history": history,
    })
    _save_manifest(manifest)
    print(f"\nApplied. {INDEX_PATH} sha256={new_sha}")
    print(f"          {MANIFEST_PATH} updated.")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="Bootstrap the manifest from the current index file.")
    pi.add_argument("--force", action="store_true",
                    help="Overwrite an existing manifest.")
    pi.set_defaults(func=cmd_init)

    pc = sub.add_parser("check", help="Verify the current index file's sha256 against the manifest.")
    pc.set_defaults(func=cmd_check)

    pr = sub.add_parser("rebuild",
                        help="Pull FSDS quarters, build a candidate index, diff, optionally apply.")
    pr.add_argument("quarters", nargs="+",
                    help="FSDS quarters, e.g. 2025q4 2026q1. Older first or newer first; "
                         "the classifier sorts newest-first internally.")
    pr.add_argument("--source-zip", action="append", metavar="QUARTER=PATH",
                    help="Use a local FSDS zip for that quarter (skip download). "
                         "Repeat per quarter.")
    pr.add_argument("--apply", action="store_true",
                    help="Write the new index and update the manifest (default: dry-run).")
    pr.add_argument("--report", help="Write a JSON diff report to this path.")
    pr.set_defaults(func=cmd_rebuild)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
