"""Deterministic Markdown rendering with complete JSON evidence."""

import json
import re


def markdown(report: dict, *, include_evidence: bool = True) -> str:
    """Render decision tables, optionally excluding the complete JSON evidence."""
    if type(include_evidence) is not bool:
        raise ValueError("include_evidence must be a boolean")

    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["# Lighthouse Noise Gate", "", f"Decision: **{report['decision']}** (exit {report['exit_code']})", "",
             ("Confidence gate: absolute tolerance applied to the adverse-change interval; MAD evidence retained."
              if report.get("confidence", {}).get("affects_gate") else
              "Raw MAD heuristic only; no significance or real-site improvement claim."), "",
             "## Group decisions", "",
             "| Group | URL | Device | Baseline runs | Current runs | Decision |",
             "| --- | --- | --- | --- | --- | --- |"]
    for group in report.get("groups", []):
        identity = group["identity"]
        metric = group["metrics"][0]
        lines.append("| " + " | ".join(map(cell, [group['id'], identity['finalDisplayedUrl'],
            identity['configSettings']['formFactor'], metric['baseline']['run_count'],
            metric['current']['run_count'], group['decision']])) + " |")
    if report.get("mismatches") or "mismatch_summary" in report:
        lines.extend(["", "## Profile mismatches", "",
                      ("Diagnostic candidates only: possible pairs share exact URLs and formFactor."
                       if "mismatch_summary" in report else
                       "Diagnostic candidates only: all pairs sharing exact URLs and formFactor are listed."),
                      ("Unmatched groups remain invalid; no cross-profile comparisons or sample pooling are performed."
                       if "mismatch_summary" in report else
                       "Groups remain invalid; no cross-profile comparisons or sample pooling are performed."), ""])
        if "mismatch_summary" in report:
            counts = report["mismatch_summary"]
            lines.extend([f"Candidate pairs: total **{counts['total']}**, emitted **{counts['emitted']}**, "
                          f"omitted **{counts['omitted']}** (limit {counts['limit']}; "
                          f"truncated: {str(counts['truncated']).lower()}).", ""])
            if "groups" in counts:
                lines.extend(["Per-group counts describe possible counterparts, not inferred matches.", "",
                              "| Unmatched group | Side | Total candidates | Emitted | Omitted | Status |",
                              "| --- | --- | --- | --- | --- | --- |"])
                for row in counts["groups"]:
                    status = ("no exact-anchor candidates" if not row["total"] else
                              "truncated" if row["truncated"] else "complete")
                    lines.append("| " + " | ".join(map(cell, [row['group_id'], row['side'], row['total'],
                        row['emitted'], row['omitted'], status])) + " |")
                lines.append("")
        if any("changes_summary" in pair for pair in report["mismatches"]):
            lines.extend(["Change-record counts apply to each emitted candidate; omitted changes are not inferred.", "",
                          "| Baseline group | Current group | Total changes | Emitted | Omitted | Limit | Truncated |",
                          "| --- | --- | --- | --- | --- | --- | --- |"])
            for pair in report["mismatches"]:
                if "changes_summary" in pair:
                    counts = pair["changes_summary"]
                    lines.append("| " + " | ".join(map(cell, [pair['baseline_group_id'], pair['current_group_id'],
                        counts['total'], counts['emitted'], counts['omitted'], counts['limit'],
                        str(counts['truncated']).lower()])) + " |")
            lines.append("")
        lines.extend([
                      "| Baseline group | Current group | Changed field (JSON pointer) | Baseline | Current |",
                      "| --- | --- | --- | --- | --- |"])
        for pair in report["mismatches"]:
            for change in pair["changes"]:
                def value(side: str) -> str:
                    return (json.dumps(change[side], ensure_ascii=True, sort_keys=True, allow_nan=False)
                            if change[side + "_present"] else "(missing)")
                lines.append("| " + " | ".join(map(cell, [pair['baseline_group_id'], pair['current_group_id'],
                    change['path'], value('baseline'), value('current')])) + " |")
    lines.extend(["", "## Metric decisions", "",
                  "| Group | Audit | Baseline n / median / MAD | Current n / median / MAD | Delta | Decision |",
                  "| --- | --- | --- | --- | --- | --- |"])
    for group in report.get("groups", []):
        for metric in group["metrics"]:
            def stats(side: str) -> str:
                data = metric[side]
                return f"{data['count']} / {data['median']} / {data['mad']}"
            lines.append("| " + " | ".join(map(cell, [group['id'], metric['audit'], stats('baseline'),
                                                      stats('current'), metric['delta'], metric['decision']])) + " |")
    if "confidence" in report:
        settings = report["confidence"]
        lines.extend(["", "## Confidence summaries", "",
                      f"Requested level: {settings['level']}; minimum {settings['min_samples_per_side']} samples per side.",
                      "Exact binomial median intervals with Bonferroni coverage per group/metric; iid within each side required.",
                      ("Confidence gate active: upper <= tolerance passes; lower > tolerance regresses; otherwise inconclusive."
                       if settings["affects_gate"] else
                       "Diagnostic only: these summaries do not change the MAD gate or exit code."), "",
                      "| Group | Audit | Status | Median change interval | Adverse change interval | Coverage lower bound |",
                      "| --- | --- | --- | --- | --- | --- |"])
        for group in report.get("groups", []):
            for metric in group["metrics"]:
                data = metric["confidence"]
                lines.append("| " + " | ".join(map(cell, [group['id'], metric['audit'], data['status'],
                    data['delta_interval'], data['adverse_delta_interval'], data['coverage_lower_bound']])) + " |")
    if not include_evidence:
        errors = report.get("errors", [])
        warnings = []
        for side in ("baseline", "current"):
            for run in report.get("runs", {}).get(side, []):
                items = run.get("runWarnings", [])
                # Projection retains raw JSON: preserve unexpected non-array payloads
                # as one value rather than dropping them or iterating strings/objects.
                if not isinstance(items, list):
                    items = [items]
                for warning in items:
                    message = (warning if isinstance(warning, str) else
                               json.dumps(warning, ensure_ascii=True, sort_keys=True, allow_nan=False))
                    warnings.append((side, run["file"], message))
        lines.extend(["", "## Input diagnostics", "",
                      f"Report errors: **{len(errors)}**. Run warnings: **{len(warnings)}**.",
                      "Errors retain report order and source prefixes; run/group copies are not counted again.",
                      "Warnings retain baseline/current, run and entry order, including duplicates. "
                      "A non-array runWarnings payload is shown as one raw value."])
        if errors:
            lines.extend(["", "| Report error |", "| --- |"])
            lines.extend("| " + cell(error) + " |" for error in errors)
        if warnings:
            lines.extend(["", "| Side | File | Warning |", "| --- | --- | --- |"])
            lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in warnings)
        reasons = [(group["id"], metric["audit"], reason)
                   for group in report.get("groups", []) for metric in group["metrics"]
                   for reason in metric["reasons"]]
        lines.extend(["", "## Metric decision reasons", "",
                      f"Metric reason occurrences: **{len(reasons)}**.",
                      "Rows retain metric.reasons in group, metric and reason order, including duplicates. "
                      "These explain the active gate; retained MAD reasons and separate confidence diagnostics "
                      "are not added."])
        if reasons:
            lines.extend(["", "| Group | Audit | Reason |", "| --- | --- | --- |"])
            lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in reasons)
        if "confidence" in report:
            diagnostics = [(group["id"], metric["audit"], reason)
                           for group in report.get("groups", []) for metric in group["metrics"]
                           for reason in metric["confidence"]["reasons"]]
            lines.extend(["", "## Confidence diagnostic reasons", "",
                          f"Confidence diagnostic reason occurrences: **{len(diagnostics)}**.",
                          "Rows retain metric.confidence.reasons in group, metric and reason order, including duplicates. "
                          "These are separate diagnostics; active gate reasons are not added."])
            if diagnostics:
                lines.extend(["", "| Group | Audit | Reason |", "| --- | --- | --- |"])
                lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in diagnostics)
        if "confidence" in report:
            intervals = []
            for group in report.get("groups", []):
                for metric in group["metrics"]:
                    for side in ("baseline", "current"):
                        data = metric["confidence"][side]
                        intervals.append((group["id"], metric["audit"], side,
                                          *("null" if value is None else value for value in
                                            ((None, None) if data is None else
                                             (data["interval"], data["order_statistic_ranks"])))))
            lines.extend(["", "## Confidence population-median evidence", "",
                          f"Population-median evidence rows: **{len(intervals)}**.",
                          "Rows retain metric.confidence.baseline/current interval and order_statistic_ranks "
                          "in group and metric order, baseline then current, including duplicates. "
                          "Null sides or fields are shown as null; no endpoints, ranks or availability are inferred. "
                          "These are population-median intervals, separate from the change interval and MAD operands. "
                          "Coverage is per group/metric; iid observations from a fixed distribution within each side "
                          "are required, with no simultaneous or real-site guarantee."])
            if intervals:
                lines.extend(["", "| Group | Audit | Side | Population-median interval | Order-statistic ranks |",
                              "| --- | --- | --- | --- | --- |"])
                lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in intervals)
        if report.get("confidence", {}).get("affects_gate"):
            mad_decisions = [(group["id"], metric["audit"], metric["mad_decision"])
                             for group in report.get("groups", []) for metric in group["metrics"]]
            mad_reasons = [(group["id"], metric["audit"], reason)
                           for group in report.get("groups", []) for metric in group["metrics"]
                           for reason in metric["mad_reasons"]]
            lines.extend(["", "## Retained MAD evidence", "",
                          f"Retained MAD decision occurrences: **{len(mad_decisions)}**. "
                          f"MAD reason occurrences: **{len(mad_reasons)}**.",
                          "Rows retain metric.mad_decision and metric.mad_reasons in group, metric and reason order, "
                          "including duplicates. These are retained heuristic evidence; the confidence gate is active."])
            if mad_decisions:
                lines.extend(["", "| Group | Audit | MAD decision |", "| --- | --- | --- |"])
                lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in mad_decisions)
            if mad_reasons:
                lines.extend(["", "| Group | Audit | Reason |", "| --- | --- | --- |"])
                lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in mad_reasons)
        relative_deltas = [(group["id"], metric["audit"],
                            "null" if metric["relative_delta"] is None else metric["relative_delta"],
                            "null" if metric["relative_delta_reason"] is None else metric["relative_delta_reason"])
                           for group in report.get("groups", []) for metric in group["metrics"]]
        lines.extend(["", "## Relative deltas", "",
                      f"Relative delta rows: **{len(relative_deltas)}**.",
                      "Rows retain metric.relative_delta as a ratio, not a percentage, and metric.relative_delta_reason "
                      "in group and metric order. Null is shown as null; an empty reason stays blank. "
                      "No values or explanations are inferred."])
        if relative_deltas:
            lines.extend(["", "| Group | Audit | Relative delta (ratio) | Stored reason |",
                          "| --- | --- | --- | --- |"])
            lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in relative_deltas)
        mad_operands = [(group["id"], metric["audit"], metric["unit"], metric["direction"],
                         *("null" if metric[key] is None else metric[key]
                           for key in ("adverse_delta", "tolerance", "noise_margin", "regression_boundary")))
                        for group in report.get("groups", []) for metric in group["metrics"]]
        lines.extend(["", "## MAD heuristic operands", "",
                      f"MAD operand rows: **{len(mad_operands)}**.",
                      "Rows retain metric.unit and metric.direction alongside metric.adverse_delta, "
                      "metric.tolerance, metric.noise_margin and "
                      "metric.regression_boundary in group and metric order. Null is shown as null; "
                      "no values are inferred or recomputed. These are MAD heuristic operands, "
                      "not the confidence gate decision rule, including when confidence is active."])
        if mad_operands:
            lines.extend(["", "| Group | Audit | Unit | Direction | Adverse delta | Tolerance | Noise margin | Regression boundary |",
                          "| --- | --- | --- | --- | --- | --- | --- | --- |"])
            lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in mad_operands)
        lines.extend(["", "Complete evidence is omitted from this Markdown summary; use the JSON report for "
                      "every run value, group identity, warning, policy and reason.", ""])
        return "\n".join(lines)
    evidence = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False, sort_keys=True)
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", evidence)), default=0))
    lines.extend(["", "## Complete evidence", "", "Every run value, group identity, warning, policy and reason is retained below.",
                  "", fence + "json", evidence, fence, ""])
    return "\n".join(lines)
