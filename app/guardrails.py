"""Static checks on the SQL an agent decides to run.

The prompt asks the model to query well. This module checks whether it did,
because an instruction is a request and a check is a fact. Both exist on purpose:
app/prompts.py steers, and this catches what steering missed.

Two of these are the difference between a demo and an incident:

    film_attention holds 4,937,204 rows and mv_interest_by_year exists so that
    nobody has to scan it. An unfiltered scan is slow enough to blow the latency
    budget and is never the right query.

    interest figures are aggregated over a subset -- films above the measurement
    floor -- so they carry interest_sample_count, which is smaller than
    sample_count. Reporting an interest median with the ROI row count behind it
    overstates the evidence, and nothing about the result would look wrong.

Deliberately heuristic. This is regex over normalised SQL, not a parser: it is
here to catch the mistakes we know the model makes, and it is checked against a
real trace rather than trusted to be exhaustive. A missed violation is a gap in
the checks, not permission.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "etl"))
from vocab import ARCHETYPES, MOTIFS  # noqa: E402

# Written out of the schema during M1. A query naming any of these was written
# from the model's memory of an older spec, not from the DDL it was given.
REMOVED_NAMES = ("pageview_peak", "pageview_decay_days", "days_since_peak",
                 "mv_attention_curve")

WRITE_VERBS = ("insert", "alter", "drop", "create", "truncate", "delete",
               "update", "optimize", "rename", "attach", "detach", "grant",
               "system")

SEVERITY_VIOLATION = "violation"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    detail: str


def normalise(sql: str) -> str:
    """Strip comments and collapse whitespace, preserving case-insensitivity."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"\s+", " ", sql).strip()


def _has_film_filter(sql: str) -> bool:
    """Whether a film_attention read is pinned to specific films.

    A join to `films` counts: the planner still restricts by key, and the agent
    doing an explicit join is not the unbounded scan this rule is about.
    """
    return bool(
        re.search(r"film_id\s*(=|in\b)", sql, re.I)
        or re.search(r"\bjoin\b[^()]*\bfilms\b", sql, re.I)
    )


def inspect(sql: str) -> list[Finding]:
    """Findings for one query. Empty means nothing known-bad was spotted."""
    findings: list[Finding] = []
    s = normalise(sql)
    low = s.lower()

    # --- writes -------------------------------------------------------------
    first = low.split("(")[0].strip()
    for verb in WRITE_VERBS:
        if re.match(rf"^{verb}\b", first):
            findings.append(Finding(
                SEVERITY_VIOLATION, "write_attempt",
                f"statement begins with {verb.upper()}; the connection is "
                "read-only and this must never be attempted"))
            break

    # --- columns and views that no longer exist -----------------------------
    for name in REMOVED_NAMES:
        if name in low:
            findings.append(Finding(
                SEVERITY_VIOLATION, "removed_name",
                f"{name} was removed from the schema in M1; the model is "
                "querying from memory rather than from the DDL it was given"))

    # --- the 4.9M-row table -------------------------------------------------
    if re.search(r"\bfrom\s+film_attention\b", low) and not _has_film_filter(s):
        findings.append(Finding(
            SEVERITY_VIOLATION, "unbounded_attention_scan",
            "reads film_attention (4,937,204 rows) without a film_id filter; "
            "mv_interest_by_year exists for exactly this"))

    # --- the two sample counts are not interchangeable ----------------------
    if "interest_pct_median" in low and "interest_sample_count" not in low:
        findings.append(Finding(
            SEVERITY_VIOLATION, "interest_without_its_count",
            "selects interest_pct_median but not interest_sample_count; an "
            "interest figure reported against the ROI sample_count overstates "
            "how much evidence stands behind it"))

    # --- ClickHouse will reject this, but naming it is more useful ----------
    # `low` is already lowercased, so the pattern must be too -- searching it
    # for a capital-M "Merge" is how this rule silently matched nothing.
    if re.search(r"\b(sum|avg|min|max|count|any|quantile\w*)\s*\(\s*\w+merge\s*\(",
                 low):
        findings.append(Finding(
            SEVERITY_VIOLATION, "nested_merge",
            "wraps a -Merge function in another aggregate; ClickHouse rejects "
            "this as ILLEGAL_AGGREGATION, resolve it in a subquery first"))

    # --- reading an aggregate state as if it were a number ------------------
    if re.search(r"\bfrom\s+mv_", low) and "merge(" not in low:
        state_cols = [c for c in ("sample_count", "roi_median", "roi_p75",
                                  "avg_tone", "interest_pct_median",
                                  "total_views", "avg_daily_views")
                      if c in low]
        if state_cols:
            findings.append(Finding(
                SEVERITY_VIOLATION, "raw_state_selected",
                f"selects {', '.join(state_cols)} from an AggregatingMergeTree "
                "view with no -Merge; this returns binary state, not numbers"))

    # --- warnings: legal, but the shape we told it to avoid -----------------
    if re.search(r"release_bucket\s*(=|in\b)", low) and not re.search(
            r"\bhaving\b", low):
        findings.append(Finding(
            SEVERITY_WARNING, "narrow_without_floor",
            "filters on release_bucket with no HAVING on the sample count; "
            "25% of archetype x bucket cells sit under the evidence floor"))

    if re.search(r"interest_median_daily\s*(>|<|=)", low) or re.search(
            r"order\s+by\s+[^;]*interest_median_daily", low):
        if "interest_cohort_pct" not in low:
            findings.append(Finding(
                SEVERITY_WARNING, "raw_interest_comparison",
                "compares or ranks on raw interest_median_daily; it spans "
                "13-40x within one cohort, so interest_cohort_pct is the "
                "comparable column"))

    return findings


def is_error_response(payload: str) -> bool:
    """Whether an MCP tool response carries a failure.

    Written after the first version -- `'"error"' in payload.lower()` -- was
    found to return False for a real mcp-clickhouse error. The success payload
    contains `"isError": false` and the failure payload `"isError": true`, and
    lowercasing turns both into `"iserror"`, so the substring `"error"` with its
    opening quote matches neither. The check reported zero errors regardless of
    what happened, which is worse than not checking.
    """
    low = payload.lower()
    if '"iserror": true' in low or '"iserror":true' in low:
        return True
    # ClickHouse errors arrive as "Code: 47. DB::Exception: ..." inside the
    # response text even when the transport considers the call successful.
    return bool(re.search(r"\bcode:\s*\d+\b", low)
                or "db::exception" in low)


def unsupported_terms(synthesis: str, evidence_text: str) -> list[str]:
    """Vocabulary terms asserted in prose that no query or result mentions.

    This catches the failure the whole project is built to avoid. In the first
    Phase A run the agent wrote that `impossible_heist` with `redemption`
    "showed a very high ROI in initial broad searches, but it was found to have
    fewer than 8 samples" -- and neither term appears in any query it ran or any
    result it received. The sentence is plausible, correctly hedged, and
    entirely invented.

    A claim about a combination is only worth anything if the rows behind it
    were actually fetched, so a term that appears only in the summary is
    reported as unsupported. Comparing against the closed vocabulary keeps this
    specific: these are exact tokens like `impossible_heist`, not English words
    the model might reasonably use in passing.
    """
    haystack = evidence_text.lower()
    prose = synthesis.lower()

    unsupported = []
    for term in set(MOTIFS) | set(ARCHETYPES):
        if term in haystack:
            continue
        # Half the vocabulary is also ordinary English -- "revenge",
        # "survival", "redemption". A model writing "a story of redemption" is
        # not citing a tag, and flagging it would mean flagging correct prose.
        # So a bare single word only counts when it is marked as a citation
        # (backticks or quotes), while an underscored token like
        # `impossible_heist` is unambiguous however it is written.
        if "_" in term:
            found = re.search(rf"\b{re.escape(term)}\b", prose)
        else:
            found = re.search(rf"[`\'\"]{re.escape(term)}[`\'\"]", prose)
        if found:
            unsupported.append(term)
    return sorted(unsupported)


def violations(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == SEVERITY_VIOLATION]


def summarise(findings: list[Finding]) -> str:
    if not findings:
        return "OK"
    return "; ".join(f"{f.severity}:{f.rule}" for f in findings)


__all__ = ["Finding", "inspect", "violations", "summarise", "normalise",
           "is_error_response", "unsupported_terms",
           "REMOVED_NAMES", "SEVERITY_VIOLATION", "SEVERITY_WARNING"]
