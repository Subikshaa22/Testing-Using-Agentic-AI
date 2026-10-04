"""Builds report.md + summary.json for a run (feeds sections 2-4 of the project report)."""
import json
import os

from . import prompts
from .agents import TestGeneratorAgent
from .stats import wilson_interval


def _mean(xs):
    return round(sum(xs) / len(xs), 2) if xs else 0.0


def _pct(a, b):
    return round(100.0 * a / b, 2) if b else 0.0


def _sum_counts(records):
    total = {"OK": 0, "CODE_BUG": 0, "TEST_BUG": 0, "SUSPECT_TEST": 0}
    for r in records:
        suites = r["samples"] if r["mode"] == "statistical" else [r]
        for s in suites:
            for k, v in s["classification"]["counts"].items():
                total[k] += v
    return total


# --------------------------------------------------------------------------- #
def summarize(config, records):
    done = [r for r in records if r.get("status") == "done"]
    summary = {
        "mode": config.mode,
        "tasks": len(records),
        "completed": len(done),
        "errors": len(records) - len(done),
        "generated_code_fully_correct": sum(
            r["code_correctness"]["passed"] == r["code_correctness"]["total"] for r in done),
    }
    if config.mode in ("coverage", "property"):
        tests = sum(r["tests"]["total"] for r in done)
        passed = sum(r["tests"]["passed"] for r in done)
        valid = sum(r["classification"]["valid_tests"] for r in done)
        summary.update({
            "total_tests_generated": tests,
            "tests_passing_on_generated_code": passed,
            "tests_valid_on_reference_solution": valid,
            "test_validity_rate_%": _pct(valid, tests),
            "test_outcome_classification": _sum_counts(done),
        })
    if config.mode == "coverage":
        summary.update({
            "criterion": f"{config.criterion} >= {config.target:.0f}%",
            "suites_meeting_requirement (PASS)": sum(r["verdict"]["verdict"] == "PASS" for r in done),
            "avg_statement_coverage_%": _mean([r["coverage"]["statement"] for r in done]),
            "avg_branch_coverage_%": _mean([r["coverage"]["branch"] for r in done]),
            "avg_branch_coverage_excl_branchless_%": _mean(
                [r["coverage"]["branch"] for r in done if r["coverage"]["num_branches"]]),
            "tasks_without_branches": sum(r["coverage"]["num_branches"] == 0 for r in done),
            "avg_loop_coverage_%": _mean([r["coverage"]["loop"] for r in done if "loop" in r["coverage"]]),
            "avg_loop_coverage_excl_loopless_%": _mean(
                [r["coverage"]["loop"] for r in done if r["coverage"].get("num_loops")]),
            "tasks_without_loops": sum(r["coverage"].get("num_loops") == 0 for r in done),
        })
    elif config.mode == "property":
        verdicts = [p["verdict"] for r in done for p in r["verdict"]["properties"].values()]
        summary.update({
            "properties_total": len(verdicts),
            "properties_satisfied": verdicts.count("SATISFIED"),
            "properties_violated": verdicts.count("VIOLATED"),
            "properties_untested": verdicts.count("UNTESTED"),
            "tasks_all_properties_satisfied (PASS)": sum(r["verdict"]["verdict"] == "PASS" for r in done),
        })
    elif config.mode == "statistical":
        n_total = sum(r["statistics"]["n"] for r in done)
        c_total = sum(r["statistics"]["correct_suites"] for r in done)
        lo, hi = wilson_interval(c_total, n_total)
        summary.update({
            "samples_per_task (n)": config.samples,
            "total_suites_generated": n_total,
            "correct_suites": c_total,
            "per_suite_correctness_p": round(c_total / n_total, 4) if n_total else 0.0,
            "p_95%_wilson_ci": [round(lo, 4), round(hi, 4)],
        })
        for k in config.ks:
            if k <= config.samples:
                summary[f"mean_pass@{k}"] = round(_mean(
                    [r["statistics"]["pass_at_k"][f"pass@{k}"] for r in done]), 4)
        summary.update({
            "tasks_with_at_least_one_correct_suite": sum(r["statistics"]["correct_suites"] > 0 for r in done),
            "mean_statement_coverage_%": _mean([r["statistics"]["mean_statement_coverage"] for r in done]),
            "mean_branch_coverage_%": _mean([r["statistics"]["mean_branch_coverage"] for r in done]),
            "mean_loop_coverage_%": _mean([r["statistics"]["mean_loop_coverage"] for r in done
                                           if "mean_loop_coverage" in r["statistics"]]),
            "mean_test_validity_rate_%": _mean([r["statistics"]["mean_validity_rate"] for r in done]),
            "test_outcome_classification": _sum_counts(done),
        })
    summary["by_complexity_tier"] = _by_tier(config, done)
    return summary


def _by_tier(config, done):
    out = {}
    for t in ("simple", "medium", "complex"):
        rs = [r for r in done if r.get("tier") == t]
        if not rs:
            continue
        entry = {"tasks": len(rs),
                 "code_correct": sum(r["code_correctness"]["passed"] == r["code_correctness"]["total"] for r in rs)}
        if config.mode == "coverage":
            entry.update(PASS=sum(r["verdict"]["verdict"] == "PASS" for r in rs),
                         avg_stmt=_mean([r["coverage"]["statement"] for r in rs]),
                         avg_branch=_mean([r["coverage"]["branch"] for r in rs]),
                         avg_loop=_mean([r["coverage"]["loop"] for r in rs if "loop" in r["coverage"]]),
                         validity=_pct(sum(r["classification"]["valid_tests"] for r in rs),
                                       sum(r["tests"]["total"] for r in rs)))
        elif config.mode == "property":
            pv = [p["verdict"] for r in rs for p in r["verdict"]["properties"].values()]
            entry.update(PASS=sum(r["verdict"]["verdict"] == "PASS" for r in rs),
                         properties_satisfied=f"{pv.count('SATISFIED')}/{len(pv)}",
                         validity=_pct(sum(r["classification"]["valid_tests"] for r in rs),
                                       sum(r["tests"]["total"] for r in rs)))
        else:
            entry.update(correct_suites=f"{sum(r['statistics']['correct_suites'] for r in rs)}/"
                                        f"{sum(r['statistics']['n'] for r in rs)}",
                         mean_pass_at_1=round(_mean([r["statistics"]["pass_at_k"]["pass@1"] for r in rs]), 4),
                         mean_branch=_mean([r["statistics"]["mean_branch_coverage"] for r in rs]))
        out[t] = entry
    return out


# --------------------------------------------------------------------------- #
def build_report(run_dir, config, records):
    summary = summarize(config, records)
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"config": config.to_dict(), "summary": summary, "records": records}, f, indent=2)

    cfg = config
    requirement = {
        "coverage": f"(1) Coverage criterion: **{cfg.criterion}**, target {cfg.target:.0f}%",
        "property": "(2) User-specified properties (see per-task details)",
        "statistical": f"(3) Statistical estimation: n = {cfg.samples} suites per task, pass@k for k in {list(cfg.ks)}",
    }[cfg.mode]
    md = [
        "# Agentic AI Unit Testing - Run Report", "",
        "## Pipeline configuration", "",
        "| Setting | Value |", "|---|---|",
        "| Dataset | MBPP (Mostly Basic Python Problems) |",
        f"| Model | `{cfg.model}` ({cfg.provider}) |",
        f"| Endpoint | `{cfg.base_url}` |",
        f"| Test requirement | {requirement} |",
        "| Pipeline | linear: code generator -> test generator -> test executor (no feedback) |",
        f"| Code generator settings | {cfg.code_settings.to_dict()} |",
        f"| Test generator settings | {cfg.test_settings.to_dict()}"
        + (" (seed + i for sample i)" if cfg.mode == "statistical" else "") + " |",
        f"| Test executor | deterministic: pytest + coverage.py (--branch) + loop-iteration counters (0/1/2+), timeout {cfg.exec_timeout}s |",
        "",
        "## Aggregate results", "",
        "| Metric | Value |", "|---|---|",
    ]
    md += [f"| {k.replace('_', ' ')} | {v} |" for k, v in summary.items() if k != "by_complexity_tier"]
    tiers = summary["by_complexity_tier"]
    if tiers:
        cols = list(next(iter(tiers.values())).keys())
        md += ["", "### Results by complexity tier", "",
               "Tier = cyclomatic complexity (CC) of the MBPP reference solution: "
               "simple CC 1-2, medium CC 3-4, complex CC >= 5.", "",
               "| Tier | " + " | ".join(c.replace("_", " ") for c in cols) + " |",
               "|---|" + "---|" * len(cols)]
        md += [f"| {t} | " + " | ".join(str(e.get(c, "")) for c in cols) + " |" for t, e in tiers.items()]
    md += ["", "Test outcome classification (each test is run on the generated code and on the MBPP "
           "reference solution): OK = passes both; CODE_BUG = fails only on generated code; "
           "TEST_BUG = fails on both (wrong expectation); SUSPECT_TEST = passes only on generated code.", ""]

    md += ["## Per-task results", ""] + _task_table(cfg, records)
    md += ["", "## Per-task details", ""] + _task_details(cfg, records)

    md += [
        "## Prompts", "",
        "Full request/response logs for every agent call are in `prompts.jsonl`.", "",
        "### Code generator - system prompt", "```text", prompts.CODE_GEN_SYSTEM, "```",
        "### Code generator - user prompt template", "```text", prompts.CODE_GEN_USER, "```",
        "### Test generator - system prompt", "```text",
        TestGeneratorAgent(None, None, cfg.mode, cfg.criterion, cfg.target).system_prompt(), "```",
        "### Test generator - user prompt template", "```text",
        (prompts.TEST_GEN_USER_BLACKBOX + prompts.TEST_GEN_USER_PROPERTIES if cfg.mode == "property"
         else prompts.TEST_GEN_USER), "```",
    ]
    with open(os.path.join(run_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    return summary


def _task_table(cfg, records):
    if cfg.mode == "coverage":
        rows = ["| Task | Tier (CC) | Function | Code vs MBPP asserts | Tests pass (generated) | Valid tests (reference) "
                "| Stmt cov % | Branch cov % | Branches | Loop cov % | Loops | Verdict |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    elif cfg.mode == "property":
        rows = ["| Task | Tier (CC) | Function | Code vs MBPP asserts | Tests pass (generated) | Valid tests (reference) "
                "| Properties (S/V/U) | Verdict |", "|---|---|---|---|---|---|---|---|"]
    else:
        rows = ["| Task | Tier (CC) | Function | Code vs MBPP asserts | Correct suites (c/n) | "
                + " | ".join(f"pass@{k}" for k in cfg.ks if k <= cfg.samples)
                + " | Mean branch cov % | Mean loop cov % | Mean validity % |",
                "|---|---|---|---|---|" + "---|" * len([k for k in cfg.ks if k <= cfg.samples]) + "---|---|---|"]
    for r in records:
        if r.get("status") != "done":
            rows.append(f"| {r['task_id']} | {r.get('tier', '')} ({r.get('complexity', '')}) | "
                        f"`{r.get('function')}` | ERROR: {r.get('error', '')[:80]} |")
            continue
        cc = r["code_correctness"]
        head = (f"| {r['task_id']} | {r.get('tier', '')} ({r.get('complexity', '')}) | `{r['function']}` | "
                f"{cc['passed']}/{cc['total']} |")
        if cfg.mode == "coverage":
            t, c, v = r["tests"], r["coverage"], r["classification"]
            rows.append(f"{head} {t['passed']}/{t['total']} | {v['valid_tests']}/{t['total']} | "
                        f"{c['statement']} | {c['branch']} | {c['num_branches']} | {c.get('loop', '-')} | "
                        f"{c.get('num_loops', '-')} | **{r['verdict']['verdict']}** |")
        elif cfg.mode == "property":
            t, v = r["tests"], r["classification"]
            pv = [p["verdict"] for p in r["verdict"]["properties"].values()]
            rows.append(f"{head} {t['passed']}/{t['total']} | {v['valid_tests']}/{t['total']} | "
                        f"{pv.count('SATISFIED')}/{pv.count('VIOLATED')}/{pv.count('UNTESTED')} | "
                        f"**{r['verdict']['verdict']}** |")
        else:
            s = r["statistics"]
            rows.append(f"{head} {s['correct_suites']}/{s['n']} | "
                        + " | ".join(str(s["pass_at_k"][f"pass@{k}"]) for k in cfg.ks if k <= cfg.samples)
                        + f" | {s['mean_branch_coverage']} | {s.get('mean_loop_coverage', '-')} | "
                          f"{s['mean_validity_rate']} |")
    return rows


def _failing(suite):
    labels = suite["classification"]["labels"]
    out = []
    for case in suite["tests"]["cases"]:
        label = labels.get(case["name"], "")
        if label and label != "OK":
            msg = (case.get("message") or "passes on generated code, fails on reference").replace("\n", " ")
            out.append(f"  - `{case['name']}` [{label}]: {msg[:160]}")
    return out


def _task_details(cfg, records):
    md = []
    for r in records:
        md.append(f"### Task {r['task_id']}: {r['problem']}")
        if r.get("status") != "done":
            md += [f"Error: {r.get('error')}", ""]
            continue
        if cfg.mode == "coverage":
            v = r["verdict"]
            md.append(f"- Verdict: **{v['verdict']}**" + (f" ({'; '.join(v['reasons'])})" if v["reasons"] else ""))
            if v["vacuous_branch_coverage"]:
                md.append("- Note: the generated code has no decision points, so branch coverage is trivially 100%.")
            if r["coverage"]["missing_lines"]:
                md.append(f"- Statements never executed: lines {r['coverage']['missing_lines']}")
            if r["coverage"]["missing_branches"]:
                md.append(f"- Branches never taken (from -> to): {r['coverage']['missing_branches']}")
            if v.get("vacuous_loop_coverage") and cfg.criterion in ("loop", "all"):
                md.append("- Note: the generated code has no for/while loops, so loop coverage is trivially 100%.")
            for m in r["coverage"].get("missing_loop_cases", []):
                md.append(f"- Loop at line {m['line']} (`{m['source']}`) never ran: "
                          + ", ".join({"0": "0 times", "1": "exactly once", "2+": "2+ times"}[x] for x in m["missing"]))
            md += _failing(r)
        elif cfg.mode == "property":
            md += ["", "| Property | Tests | Passed | Failed | Verdict |", "|---|---|---|---|---|"]
            for k, p in r["verdict"]["properties"].items():
                md.append(f"| {k}: {p['property']} | {len(p['tests'])} | {p['passed']} | {p['failed']} | "
                          f"**{p['verdict']}** |")
            md.append("")
            if r["verdict"]["unmapped_tests"]:
                md.append(f"- Tests not linked to any property: {r['verdict']['unmapped_tests']}")
            md += _failing(r)
        else:
            md += ["", "| Sample | Tests pass (generated) | Valid (reference) | Stmt % | Branch % | Loop % | Correct suite |",
                   "|---|---|---|---|---|---|---|"]
            for s in r["samples"]:
                t = s["tests"]
                md.append(f"| {s['sample']} | {t['passed']}/{t['total']} | "
                          f"{s['classification']['valid_tests']}/{t['total']} | {s['coverage']['statement']} | "
                          f"{s['coverage']['branch']} | {s['coverage'].get('loop', '-')} | "
                          f"{'yes' if s['correct'] else 'no'} |")
            md.append("")
        folder = f"task_{r['task_id']}"
        md.append(f"- Artifacts: `{folder}/solution.py`, `{folder}/"
                  + ("sample_<i>/" if cfg.mode == "statistical" else "") + "test_solution.py`, "
                  f"`{folder}/verdict.json`")
        md.append("")
    return md
