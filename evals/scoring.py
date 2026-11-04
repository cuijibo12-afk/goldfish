"""Deterministic, conservative probes for the labelled memory corpus.

These are evidence/phrase checks, not semantic grades.  An evidence-backed
paraphrase that misses a hand-written phrase stays unadjudicated.  Precision
counts returned records; recall counts distinct expected gold facts.  The two
denominators must not be mixed.
"""
from __future__ import annotations

import html
import json
import math
import unicodedata
from collections import Counter
from typing import Any


def _normal(text: Any) -> str:
    return "".join(unicodedata.normalize("NFKC", html.unescape(str(text or ""))).casefold().split())


def _ids(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return {value} if value else set()
        value = decoded if isinstance(decoded, list) else [value]
    return {str(item) for item in (value or []) if isinstance(item, (str, int)) and str(item)}


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _groups(text: str, groups: list) -> dict:
    normalized = _normal(text)
    rows = []
    for group in groups:
        terms = [group] if isinstance(group, str) else group
        terms = [str(term) for term in (terms or []) if _normal(term)]
        hits = [term for term in terms if _normal(term) in normalized]
        rows.append({"alternatives": terms, "matched_terms": hits, "matched": bool(hits)})
    return {"matched": sum(row["matched"] for row in rows), "total": len(rows),
            "complete": all(row["matched"] for row in rows), "groups": rows}


def _unique(records: list[dict], prefix: str) -> list[dict]:
    """Ignore repeated IDs so retries cannot improve a metric."""
    seen = set()
    result = []
    for index, original in enumerate(records or []):
        row = dict(original)
        key = str(row.get("id") or row.get("summary_id") or row.get("profile_id") or f"{prefix}:{index}")
        if key in seen:
            continue
        seen.add(key)
        row["id"] = key
        result.append(row)
    return result


def _source_ids(corpus: dict) -> set[str]:
    """Use actual messages when supplied, never the incomplete gold ID union."""
    result = set()
    for row in corpus.get("messages", corpus.get("raw_messages", [])):
        if isinstance(row, dict) and row.get("id", row.get("message_id")):
            result.add(str(row.get("id", row.get("message_id"))))
    for session in corpus.get("sessions", []):
        for row in session.get("messages", []):
            if row.get("id", row.get("message_id")):
                result.add(str(row.get("id", row.get("message_id"))))
    return result


def _match_records(records: list[dict], gold: list[dict], id_key: str,
                   source_ids: set[str]) -> tuple[list[dict], list[dict]]:
    record_rows = []
    for row in records:
        evidence = _ids(row.get("evidence_ids"))
        candidates = []
        for item in gold:
            overlap = evidence & _ids(item.get("evidence_ids"))
            if not overlap:
                continue
            details = _groups(row.get("text", ""), item.get("required_detail_groups", []))
            candidates.append({"gold_id": item[id_key], "evidence_overlap": sorted(overlap),
                               "details": details, "expected_kind": item.get("kind")})
        matched = sorted(candidate["gold_id"] for candidate in candidates if candidate["details"]["complete"])
        invalid = sorted(evidence - source_ids) if source_ids else []
        if invalid:
            matched = []
        status = ("unsupported_no_evidence" if not evidence else
                  "invalid_evidence" if invalid else
                  "probe_match" if matched else "manual_review")
        record_rows.append({"id": row["id"], "kind": row.get("kind"), "status": status,
                            "text": row.get("text", ""), "start_time": row.get("start_time"),
                            "end_time": row.get("end_time"), "theme_titles": row.get("theme_titles"),
                            "evidence_ids": sorted(evidence), "invalid_evidence_ids": invalid,
                            "matched_gold_ids": matched,
                            "candidate_gold_ids": sorted(candidate["gold_id"] for candidate in candidates),
                            "candidates": candidates})
    gold_rows = []
    for item in gold:
        gid = item[id_key]
        candidates = [(row, candidate) for row in record_rows for candidate in row["candidates"]
                      if candidate["gold_id"] == gid]
        valid = [(row, candidate) for row, candidate in candidates
                 if row["status"] not in {"unsupported_no_evidence", "invalid_evidence"}]
        matched = [row["id"] for row, candidate in valid if candidate["details"]["complete"]]
        best = max((candidate["details"] for _, candidate in valid),
                   key=lambda details: details["matched"], default=_groups("", item.get("required_detail_groups", [])))
        gold_rows.append({"gold_id": gid, "label": item.get("label", ""),
                          "status": "probe_match" if matched else "manual_review" if valid else "no_evidence_candidate",
                          "matched_record_ids": sorted(matched),
                          "candidate_record_ids": sorted(row["id"] for row, _ in valid),
                          "detail_probe": best,
                          "time_start": item.get("time_start"), "time_end": item.get("time_end"),
                          "themes": item.get("themes", []),
                          "time_and_theme_adjudication": "not_automatically_graded"})
    return record_rows, gold_rows


def _coverage(records: list[dict], gold_rows: list[dict]) -> dict:
    matched = [row["gold_id"] for row in gold_rows if row["status"] == "probe_match"]
    review = [row["gold_id"] for row in gold_rows if row["status"] == "manual_review"]
    missing = [row["gold_id"] for row in gold_rows if row["status"] == "no_evidence_candidate"]
    details_hit = sum(row["detail_probe"]["matched"] for row in gold_rows)
    details_total = sum(row["detail_probe"]["total"] for row in gold_rows)
    return {"gold_count": len(gold_rows), "record_count": len(records),
            "probe_matched_count": len(matched), "probe_matched_ids": matched,
            "manual_review_ids": review, "no_evidence_candidate_ids": missing,
            "unmatched_ids": sorted(review + missing),
            "coverage_lower_bound": _ratio(len(matched), len(gold_rows)),
            "coverage_upper_bound": _ratio(len(matched) + len(review), len(gold_rows)),
            "detail_probe_matched_groups": details_hit, "detail_probe_total_groups": details_total,
            "detail_probe_coverage": _ratio(details_hit, details_total),
            "unsupported_record_ids": [row["id"] for row in records if row["status"] == "unsupported_no_evidence"],
            "invalid_evidence_record_ids": [row["id"] for row in records if row["status"] == "invalid_evidence"],
            "records": records, "gold_items": gold_rows}


def score_extraction(corpus: dict, summaries: list[dict]) -> dict:
    records = _unique(summaries, "summary")
    record_rows, gold_rows = _match_records(records, corpus.get("gold_facts", []), "fact_id", _source_ids(corpus))
    return _coverage(record_rows, gold_rows)


def score_profiles(corpus: dict, profiles: list[dict]) -> dict:
    profiles = _unique(profiles, "profile")
    records, gold_rows = _match_records(profiles, corpus.get("gold_profiles", []), "profile_id", _source_ids(corpus))
    expected = {row["profile_id"]: row for row in corpus.get("gold_profiles", [])}
    correct = set()
    correct_decisions = 0
    classification_errors = []
    for row in records:
        row["correctly_classified_gold_ids"] = []
        for gid in row["matched_gold_ids"]:
            if row["status"] != "probe_match":
                continue
            if row["kind"] == expected[gid].get("kind"):
                correct.add(gid)
                correct_decisions += 1
                row["correctly_classified_gold_ids"].append(gid)
            else:
                classification_errors.append({"record_id": row["id"], "profile_id": gid,
                                              "expected_kind": expected[gid].get("kind"), "actual_kind": row["kind"]})
    result = _coverage(records, gold_rows)
    result.update({"correctly_classified_ids": sorted(correct),
                   "correctly_classified_count": len(correct), "classification_errors": classification_errors,
                   "classification_decision_count": correct_decisions + len(classification_errors),
                   "correct_classification_decision_count": correct_decisions,
                   "classification_accuracy_on_probe_matches": _ratio(correct_decisions, correct_decisions + len(classification_errors)),
                   "forbidden_profile_review": []})
    for forbidden in corpus.get("forbidden_profiles", []):
        for profile in profiles:
            overlap = _ids(profile.get("evidence_ids")) & _ids(forbidden.get("evidence_ids"))
            if not overlap:
                continue
            groups = forbidden.get("required_detail_groups", [])
            probe = _groups(profile.get("text", ""), groups) if groups else None
            if groups and not probe["complete"]:
                continue
            result["forbidden_profile_review"].append({
                "record_id": profile["id"], "forbidden_profile_id": forbidden.get("forbidden_profile_id"),
                "status": "manual_review", "phrase_probe": probe,
                "claim": forbidden.get("claim", ""), "reason": forbidden.get("reason", ""),
                "note": "Evidence overlap or phrase overlap does not establish that a forbidden interpretation was asserted."})
    return result


def _pair_matches(left: set[str], right: set[str], relation: dict) -> bool:
    a, b = relation.get("from_fact_id"), relation.get("to_fact_id")
    return (a in left and b in right) or (a in right and b in left)


def _relation_kinds(relation: dict) -> set[str]:
    kinds = relation.get("allowed_kinds", relation.get("kinds", relation.get("kind", [])))
    return {kinds} if isinstance(kinds, str) else set(kinds or [])


def score_relations(corpus: dict, links: list[dict], extraction: dict) -> dict:
    """Check links as undirected pairs: MemoryDB sorts endpoint IDs on write."""
    summaries = {row["id"]: row for row in extraction["records"]}
    source_ids = _source_ids(corpus)
    gold = corpus.get("gold_relations", [])
    rows = []
    matched_gold = set()
    for link in _unique(links, "link"):
        left_id = str(link.get("left_id", link.get("from_summary_id", "")))
        right_id = str(link.get("right_id", link.get("to_summary_id", "")))
        left, right = summaries.get(left_id, {}), summaries.get(right_id, {})
        left_facts = set(left.get("matched_gold_ids", []))
        right_facts = set(right.get("matched_gold_ids", []))
        possible_left = set(left.get("candidate_gold_ids", []))
        possible_right = set(right.get("candidate_gold_ids", []))
        evidence = _ids(link.get("evidence_ids"))
        confidence = link.get("confidence")
        confidence_valid = (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                            and math.isfinite(confidence) and 0 <= confidence <= 1)
        issues = []
        if not evidence:
            issues.append("missing_evidence")
        invalid = sorted(evidence - source_ids) if source_ids else []
        if invalid:
            issues.append("invalid_evidence")
        if not str(link.get("reason", "")).strip():
            issues.append("missing_reason")
        if not confidence_valid:
            issues.append("invalid_confidence")
        if not left or not right:
            issues.append("missing_exported_endpoint")
        if left_id == right_id:
            issues.append("self_link")
        if link.get("kind") == "causal_confirmed" and (
                not link.get("confirmed") or link.get("origin") not in {"user_confirmed", "human_review"}):
            issues.append("unconfirmed_causal_claim")
        forbidden = [item for item in corpus.get("forbidden_relations", [])
                     if _pair_matches(left_facts, right_facts, item)
                     and (not _relation_kinds(item) or link.get("kind") in _relation_kinds(item))]
        candidates = []
        for item in gold:
            if not _pair_matches(left_facts, right_facts, item):
                continue
            reason = _groups(link.get("reason", ""), item.get("required_reason_groups", []))
            needed = _ids(item.get("evidence_ids"))
            kind_matches = link.get("kind") in _relation_kinds(item)
            evidence_complete = bool(evidence) and needed <= evidence
            complete = kind_matches and reason["complete"] and evidence_complete and not issues and not forbidden
            if complete:
                matched_gold.add(item["relation_id"])
            candidates.append({"relation_id": item["relation_id"], "kind_matches": kind_matches,
                               "reason_probe": reason, "required_evidence_covered": evidence_complete,
                               "missing_evidence_ids": sorted(needed - evidence), "probe_match": complete})
        status = ("explicitly_forbidden" if forbidden else "structural_error" if issues else
                  "probe_match" if any(item["probe_match"] for item in candidates) else "manual_review")
        rows.append({"id": link["id"], "left_id": left_id, "right_id": right_id,
                     "kind": link.get("kind"), "reason": link.get("reason", ""),
                     "status": status, "structural_issues": issues,
                     "left_fact_ids": sorted(left_facts), "right_fact_ids": sorted(right_facts),
                     "possible_gold_relation_ids": [item["relation_id"] for item in gold
                                                    if _pair_matches(possible_left, possible_right, item)],
                     "has_evidence": bool(evidence), "invalid_evidence_ids": invalid,
                     "has_reason": bool(str(link.get("reason", "")).strip()),
                     "confidence": confidence if confidence_valid else None,
                     "confidence_valid": confidence_valid, "candidates": candidates,
                     "forbidden_matches": [item.get("relation_id", item.get("forbidden_relation_id", item.get("reason", "")))
                                           for item in forbidden]})
    return {"record_count": len(rows), "gold_count": len(gold),
            "status_counts": dict(Counter(row["status"] for row in rows)),
            "with_evidence_count": sum(row["has_evidence"] for row in rows),
            "with_reason_count": sum(row["has_reason"] for row in rows),
            "with_valid_confidence_count": sum(row["confidence_valid"] for row in rows),
            "probe_matched_gold_ids": sorted(matched_gold),
            "unmatched_gold_ids": sorted(set(item["relation_id"] for item in gold) - matched_gold),
            "explicitly_wrong_link_ids": [row["id"] for row in rows if row["status"] == "explicitly_forbidden"],
            "manual_review_link_ids": [row["id"] for row in rows if row["status"] == "manual_review"],
            "evidence_note": "Link evidence is a source-traceability check. MemoryDB automatically cites both endpoints; this does not prove a relationship.",
            "records": rows}


def _precision(rows: list[dict]) -> dict:
    counts = Counter(row["status"] for row in rows)
    relevant, false_positive = counts["relevant"], counts["false_positive"]
    unknown = counts["unknown"]
    return {"returned_count": len(rows), "relevant_count": relevant,
            "false_positive_count": false_positive, "unknown_count": unknown,
            "judged_count": relevant + false_positive,
            "precision": _ratio(relevant, relevant + false_positive),
            "precision_lower_bound": _ratio(relevant, len(rows)),
            "precision_upper_bound": _ratio(relevant + unknown, len(rows))}


def _returned_profiles(result: dict, profiles: list[dict]) -> tuple[list[str], list[dict]]:
    if "profile_ids" in result:
        return sorted(_ids(result["profile_ids"])), []
    if "profiles" in result:
        return list(dict.fromkeys(str(row.get("id")) if isinstance(row, dict) else str(row)
                                  for row in result["profiles"])), []
    labels = {"user_profile": "用户画像", "interaction_expectation": "交互期望", "ai_observed": "AI行为记录", "feedback": "反馈"}
    lines = html.unescape(result.get("injection", "")).splitlines()
    found, ambiguous = [], []
    for line in dict.fromkeys(lines):
        candidates = [profile["id"] for profile in profiles
                      if _normal(profile.get("text")) and _normal(line) in {
                          _normal("[" + label + "] " + profile["text"])
                          for label in {labels.get(profile.get("kind"), str(profile.get("kind"))), str(profile.get("kind"))}}]
        if len(candidates) == 1:
            found.extend(candidates)
        elif candidates:
            ambiguous.append({"injection_line": line, "candidate_record_ids": candidates})
    return list(dict.fromkeys(found)), ambiguous


def _score_query(query: dict, result: dict, summary_rows: dict, profile_rows: dict,
                 profiles: list[dict]) -> dict:
    expected = _ids(query.get("expected_fact_ids"))
    allowed = expected | _ids(query.get("allowed_fact_ids"))
    forbidden = _ids(query.get("forbidden_fact_ids"))
    retrieved, possible, items = set(), set(), []
    seen = set()
    for index, returned in enumerate(result.get("results", [])):
        sid = str(returned.get("id", returned.get("summary_id", f"missing:{index}"))) if isinstance(returned, dict) else str(returned)
        if sid in seen:
            continue
        seen.add(sid)
        record = summary_rows.get(sid)
        matched = set(record.get("matched_gold_ids", [])) if record else set()
        candidate = (set(record.get("candidate_gold_ids", [])) if record and record["status"]
                     not in {"unsupported_no_evidence", "invalid_evidence"} else set())
        retrieved |= matched
        possible |= candidate
        blocked = matched & forbidden
        unsupported = bool(record and record["status"] in {"unsupported_no_evidence", "invalid_evidence"})
        if blocked or unsupported:
            status = "false_positive"
        elif matched & allowed:
            status = "relevant"
        else:
            status = "unknown"
        items.append({"id": sid, "status": status, "matched_fact_ids": sorted(matched),
                      "candidate_fact_ids": sorted(candidate), "forbidden_fact_ids": sorted(blocked),
                      "reason": "forbidden_fact" if blocked else "unsupported_evidence" if unsupported else
                                "allowed_fact_probe_match" if status == "relevant" else
                                "missing_exported_summary" if record is None else "requires_manual_review"})
    hit = expected & retrieved
    unresolved = (expected & possible) - hit
    profile_items, profile_hits, profile_possible = [], set(), set()
    expected_profiles = _ids(query.get("expected_profile_ids"))
    allowed_profiles = expected_profiles | _ids(query.get("allowed_profile_ids"))
    forbidden_profiles = _ids(query.get("forbidden_profile_ids"))
    returned_profile_ids, ambiguous_profiles = _returned_profiles(result, profiles)
    for pid in returned_profile_ids:
        record = profile_rows.get(pid)
        matched = set(record.get("correctly_classified_gold_ids", [])) if record else set()
        candidate = ({item["gold_id"] for item in record["candidates"]
                      if record["kind"] is None or record["kind"] == item["expected_kind"]}
                     if record and record["status"] not in {"unsupported_no_evidence", "invalid_evidence"} else set())
        profile_hits |= matched
        profile_possible |= candidate
        unsupported = bool(record and record["status"] in {"unsupported_no_evidence", "invalid_evidence"})
        status = "false_positive" if matched & forbidden_profiles or unsupported else "relevant" if matched & allowed_profiles else "unknown"
        profile_items.append({"id": pid, "status": status, "matched_profile_ids": sorted(matched),
                              "candidate_profile_ids": sorted(candidate)})
    for index, ambiguous in enumerate(ambiguous_profiles):
        candidates = {item["gold_id"] for pid in ambiguous["candidate_record_ids"]
                      for item in profile_rows[pid]["candidates"]
                      if profile_rows[pid]["kind"] == item["expected_kind"]}
        profile_possible |= candidates
        profile_items.append({"id": f"ambiguous_injection_profile:{index}", "status": "unknown",
                              "matched_profile_ids": [], "candidate_profile_ids": sorted(candidates),
                              "reason": "identical_profile_text_without_record_id", **ambiguous})
    profile_hit = expected_profiles & profile_hits
    profile_unresolved = (expected_profiles & profile_possible) - profile_hit
    return {"query_id": query["query_id"], "tags": query.get("tags", []), "as_of": query.get("as_of"),
            "time_anchor": query.get("time_anchor"), "precision_unit": "unique_returned_summary",
            **_precision(items), "expected_fact_count": len(expected), "hit_fact_count": len(hit),
            "recall": _ratio(len(hit), len(expected)),
            "recall_lower_bound": _ratio(len(hit), len(expected)),
            "recall_upper_bound": _ratio(len(hit | unresolved), len(expected)),
            "hit_fact_ids": sorted(hit), "missed_fact_ids": sorted(expected - hit),
            "unresolved_fact_ids": sorted(unresolved), "no_candidate_fact_ids": sorted(expected - hit - unresolved),
            "retrieved_fact_ids": sorted(retrieved), "false_positive_ids": [row["id"] for row in items if row["status"] == "false_positive"],
            "unknown_ids": [row["id"] for row in items if row["status"] == "unknown"], "items": items,
            "profiles": {**_precision(profile_items), "expected_count": len(expected_profiles),
                         "hit_count": len(profile_hit), "recall": _ratio(len(profile_hit), len(expected_profiles)),
                         "recall_upper_bound": _ratio(len(profile_hit | profile_unresolved), len(expected_profiles)),
                         "hit_profile_ids": sorted(profile_hit), "missed_profile_ids": sorted(expected_profiles - profile_hit),
                         "unresolved_profile_ids": sorted(profile_unresolved), "items": profile_items}}


def _aggregate_queries(rows: list[dict]) -> dict:
    items = [item for row in rows for item in row["items"]]
    expected = sum(row["expected_fact_count"] for row in rows)
    hit = sum(row["hit_fact_count"] for row in rows)
    unresolved = sum(len(row["unresolved_fact_ids"]) for row in rows)
    profile_items = [item for row in rows for item in row["profiles"]["items"]]
    profile_expected = sum(row["profiles"]["expected_count"] for row in rows)
    profile_hit = sum(row["profiles"]["hit_count"] for row in rows)
    profile_unresolved = sum(len(row["profiles"]["unresolved_profile_ids"]) for row in rows)
    return {"query_count": len(rows), **_precision(items), "expected_fact_count": expected,
            "hit_fact_count": hit, "missed_fact_count": expected - hit,
            "unresolved_fact_count": unresolved, "recall": _ratio(hit, expected),
            "recall_lower_bound": _ratio(hit, expected), "recall_upper_bound": _ratio(hit + unresolved, expected),
            "profiles": {**_precision(profile_items), "expected_count": profile_expected,
                         "hit_count": profile_hit, "missed_count": profile_expected - profile_hit,
                         "unresolved_count": profile_unresolved, "recall": _ratio(profile_hit, profile_expected),
                         "recall_lower_bound": _ratio(profile_hit, profile_expected),
                         "recall_upper_bound": _ratio(profile_hit + profile_unresolved, profile_expected)}}


def score_recall(corpus: dict, recalls: dict | list, extraction: dict,
                 profile_scores: dict, profiles: list[dict]) -> dict:
    if isinstance(recalls, list):
        by_query = {row["query_id"]: row.get("recall", row.get("result", row)) for row in recalls}
    else:
        by_query = recalls or {}
    summary_rows = {row["id"]: row for row in extraction["records"]}
    profile_rows = {row["id"]: row for row in profile_scores["records"]}
    rows = []
    for query in corpus.get("queries", []):
        result = by_query.get(query["query_id"])
        if result is None:
            rows.append({"query_id": query["query_id"], "status": "not_run", "tags": query.get("tags", [])})
            continue
        row = _score_query(query, result, summary_rows, profile_rows, _unique(profiles, "profile"))
        row["status"] = "evaluated"
        rows.append(row)
    evaluated = [row for row in rows if row["status"] == "evaluated"]
    tags = sorted({tag for row in evaluated for tag in row["tags"]})
    return {"summary": _aggregate_queries(evaluated), "not_run_query_ids": [row["query_id"] for row in rows if row["status"] == "not_run"],
            "by_tag": {tag: _aggregate_queries([row for row in evaluated if tag in row["tags"]]) for tag in tags}, "queries": rows}


def score_run(corpus: dict, summaries: list[dict], profiles: list[dict],
              links: list[dict], recalls: dict | list) -> dict:
    """Score DB exports without calling a model or requiring credentials.

    ``recalls`` is ``{query_id: MemoryDB.recall(...)}``, or a list with a
    ``query_id`` and optional ``result``/``recall`` wrapper.  Exported records
    must include source ``evidence_ids``; profile evidence may be JSON text.
    No key or private configuration is read by this module.
    """
    extraction = score_extraction(corpus, summaries)
    profile_scores = score_profiles(corpus, profiles)
    return {"methodology": {
                "type": "deterministic_evidence_and_phrase_probes", "semantic_quality_grade": False,
                "note": "Phrase matches are reproducible probes, not semantic verification. Evidence-backed paraphrases and unlabelled claims require human review; no-evidence candidates are reported separately from lexical misses.",
                "precision_unit": "unique_returned_summary_per_query", "recall_unit": "distinct_expected_gold_fact_per_query",
                "detail_probe_aggregation": "best_single_evidence_linked_record_per_gold_item",
                "bounds_note": "Bounds reflect unresolved annotated phrase-probe candidates, not semantic correctness bounds, statistical confidence intervals, or exhaustive guarantees.",
                "time_and_theme_grading": "human_review", "relation_direction": "undirected_engine_storage"},
            "extraction": extraction, "profiles": profile_scores,
            "relations": score_relations(corpus, links, extraction),
            "recall": score_recall(corpus, recalls, extraction, profile_scores, profiles)}
