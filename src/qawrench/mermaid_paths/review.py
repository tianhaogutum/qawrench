"""Author review templates and binding a review to exact source bytes and edges."""
from __future__ import annotations

from typing import Any

from .errors import fail
from .i18n import interface_language
from .text import plain_text

REVIEW_SCHEMA = "mermaid-paths/review-v1"
_MISSING = object()


def _same(actual: Any, expected: Any) -> bool:
    """JavaScript ``===`` for JSON values (booleans are not numbers)."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(actual, str) and isinstance(expected, str):
        return actual == expected
    return False


def create_review(graph: dict, lang: str = "en") -> dict:
    _, _, t = interface_language(lang)
    labels = {node["id"]: node["label"] for node in graph["nodes"]}
    return {
        "schema": REVIEW_SCHEMA, "sourceSha256": graph["sourceSha256"],
        "instructions": t("reviewInstructions"),
        "edges": [{
            "id": edge["id"], "from": edge["from"], "fromLabel": labels[edge["from"]],
            "to": edge["to"], "toLabel": labels[edge["to"]], "sourceLine": edge["line"],
            "lineStyle": edge["style"], "sourceLabel": edge["label"],
            "text": edge["label"] if edge["label"] is not None else t("unlabeled"), "confirmed": False,
        } for edge in graph["edges"]],
    }


def apply_review(graph: dict, review: Any = None, draft: Any = False) -> dict:
    """Bind the human review to exact input bytes and exact expanded edges."""
    if not isinstance(draft, bool):
        fail("OPTIONS", "draft must be a boolean.")
    by_id: dict[str, dict] = {}
    if review is not None:
        if not isinstance(review, dict) or not _same(review.get("schema", _MISSING), REVIEW_SCHEMA):
            fail("REVIEW_SCHEMA", f"Expected a {REVIEW_SCHEMA} object.")
        if not _same(review.get("sourceSha256", _MISSING), graph["sourceSha256"]):
            fail("STALE_REVIEW", "Review does not match these source bytes. Generate and confirm a fresh review.")
        records = review.get("edges")
        if not isinstance(records, list) or len(records) != len(graph["edges"]):
            fail("REVIEW_EDGES", "Review must contain exactly one record for every directed connection.")
        actual = {edge["id"]: edge for edge in graph["edges"]}
        labels = {node["id"]: node["label"] for node in graph["nodes"]}
        for record in records:
            record_id = record.get("id") if isinstance(record, dict) else None
            if not isinstance(record_id, str) or record_id not in actual or record_id in by_id:
                fail("REVIEW_EDGES", "Review contains an unknown or repeated edge id.")
            edge = actual[record_id]
            if (not _same(record.get("from", _MISSING), edge["from"]) or not _same(record.get("to", _MISSING), edge["to"])
                    or not _same(record.get("sourceLabel", _MISSING), edge["label"])):
                fail("REVIEW_EDGES", f"Review endpoint or source label differs for {edge['id']}.")
            context = {"fromLabel": labels[edge["from"]], "toLabel": labels[edge["to"]],
                       "sourceLine": edge["line"], "lineStyle": edge["style"]}
            for field, expected in context.items():
                # These context fields were added to review-v1. Accept older templates,
                # but validate every context value when present so it cannot mislead.
                if field in record and not _same(record[field], expected):
                    fail("REVIEW_EDGES", f"Review context {field} differs for {edge['id']}.")
            if not isinstance(record.get("confirmed"), bool):
                fail("REVIEW_SCHEMA", f"confirmed for {edge['id']} must be true or false.")
            by_id[record_id] = {"text": plain_text(record.get("text"), f"Review text for {edge['id']}"),
                                "confirmed": record["confirmed"]}
    edges = []
    for edge in graph["edges"]:
        reviewed = by_id.get(edge["id"])
        edges.append({
            **edge,
            "text": reviewed["text"] if reviewed else (edge["label"] if edge["label"] is not None
                                                       else "Unlabeled connection"),
            "textOrigin": "review" if reviewed else ("source" if edge["label"] else "placeholder"),
            "confirmed": reviewed["confirmed"] if reviewed else False,
        })
    unconfirmed = [edge for edge in edges if not edge["confirmed"]]
    if not draft and unconfirmed:
        fail("UNCONFIRMED", f"{len(unconfirmed)} connection description(s) need author confirmation. Run review, "
             "edit the JSON, and supply --review; use --draft only for a clearly marked draft.")
    return {**graph, "edges": edges, "reviewed": not unconfirmed,
            "unconfirmed": [edge["id"] for edge in unconfirmed]}
