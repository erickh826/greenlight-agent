"""Validation helpers for schema-bound treatment proposals.

Phase B is intentionally no-tools: it receives the Phase A transcript and turns
that evidence into one TreatmentProposal. These checks make "grounded" mean a
few concrete things the runner can enforce after the model returns JSON.
"""

from __future__ import annotations

import json
import re
from json import JSONDecodeError, JSONDecoder

from app.config import MIN_SAMPLE_SIZE
from app.contracts import TreatmentProposal
from app.guardrails import inspect, normalise, unsupported_terms, violations


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1] == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def extract_json_object(text: str) -> str:
    """Return the first JSON object from model text.

    ADK output_schema normally yields raw JSON text. The fallback accepts fenced
    or lightly wrapped text so a validation failure points at the proposal
    content rather than at harmless formatting.
    """
    text = _strip_fence(text)
    try:
        decoded = json.loads(text)
    except JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        return text

    decoder = JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            decoded, _ = decoder.raw_decode(text[index:])
        except JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return json.dumps(decoded)

    raise ValueError("no JSON object found in model output")


def parse_treatment_proposal(text: str) -> TreatmentProposal:
    return TreatmentProposal.model_validate_json(extract_json_object(text))


def canonical_sql(sql: str) -> str:
    """Comparable form of a query: no comments, one space, no trailing ;."""
    return normalise(sql).rstrip(";").lower()


def source_names(sql: str) -> set[str]:
    names = set()
    for match in re.finditer(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)",
                             normalise(sql), re.I):
        names.add(match.group(1).split(".")[-1].lower())
    return names


def validate_grounded_proposal(
    proposal: TreatmentProposal,
    phase_a_transcript: str,
) -> list[str]:
    """Errors that make a Phase B proposal insufficiently grounded."""
    errors: list[str] = []
    transcript_sql = canonical_sql(phase_a_transcript)

    if proposal.variant != "grounded":
        errors.append(f"variant must be grounded, got {proposal.variant!r}")

    if not proposal.evidence:
        errors.append("proposal must include at least one evidence item")

    cited_vocab = " ".join(
        f"`{item.value}`"
        for item in [*proposal.motif_tags, *proposal.character_archetypes]
    )
    cited_text = "\n".join(
        [cited_vocab, proposal.rationale]
        + [item.claim for item in proposal.evidence]
    )
    invented = unsupported_terms(cited_text, phase_a_transcript)
    if invented:
        errors.append(
            "vocabulary not present in Phase A trace: " + ", ".join(invented)
        )

    for index, item in enumerate(proposal.evidence, start=1):
        prefix = f"evidence[{index}]"
        sql = item.sql_query.strip()
        if not sql:
            errors.append(f"{prefix} has an empty sql_query")
            continue

        if canonical_sql(sql) not in transcript_sql:
            errors.append(f"{prefix} sql_query was not copied from Phase A")

        source = item.source_view.strip().split(".")[-1].lower()
        if source and source not in source_names(sql):
            errors.append(
                f"{prefix} source_view {item.source_view!r} is not read by "
                "sql_query"
            )

        bad = violations(inspect(sql))
        if bad:
            errors.append(
                f"{prefix} repeats guarded SQL: "
                + ", ".join(f.rule for f in bad)
            )

        if item.sample_count < MIN_SAMPLE_SIZE:
            errors.append(
                f"{prefix} sample_count {item.sample_count} is below "
                f"{MIN_SAMPLE_SIZE}"
            )

    return errors


__all__ = [
    "canonical_sql",
    "source_names",
    "extract_json_object",
    "parse_treatment_proposal",
    "validate_grounded_proposal",
]
