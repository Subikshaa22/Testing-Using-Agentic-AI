import argparse
import ast
import json
import re
from pathlib import Path


# Load the reusable Copilot instructions used when building a batch prompt.
PROMPT_PATH = Path(__file__).parent / "prompts" / "test_generator_prompt.txt"
TEST_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")


# Read MBPP JSONL records and optionally stop after a requested number of records.
def load_tasks(path, limit):
    tasks = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                tasks.append(json.loads(line))
            if limit and len(tasks) >= limit:
                break
    return tasks


# Infer the requested evaluation objective from the user's natural-language request.
def infer_objective(request, selected_objective):
    if selected_objective != "auto":
        return selected_objective
    request_lower = request.lower()
    if "statistical" in request_lower:
        return "statistical"
    if "property" in request_lower:
        return "property"
    return "coverage"


# Select explicit program IDs from a request, or use the normal dataset limit.
def select_requested_tasks(tasks, request, limit):
    matches = re.findall(r"\bprogram\s+(\d+)\b", request.lower())
    if matches:
        requested_ids = {int(match) for match in matches}
        return [task for task in tasks if task.get("task_id") in requested_ids]
    return tasks[:limit] if limit else tasks


# Identify branch and loop nodes and estimate the binary path space statically.
def identify_paths(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return {"syntax_error": str(error), "branches": [], "total_paths": 0}
    branches = []
    for index, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.If):
            kind = "if"
        elif isinstance(node, ast.While):
            kind = "while"
        elif isinstance(node, ast.For):
            kind = "for"
        else:
            continue
        branches.append({"id": str(index), "kind": kind, "line": node.lineno})
    return {
        "branches": branches,
        "branch_count": len(branches),
        "total_paths": 2 ** len(branches),
    }


# Read the structured suite and execution evidence returned by Copilot or another LLM.
def load_llm_response(path):
    response = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(response, dict):
        raise ValueError("LLM response must be a JSON object keyed by task ID")
    return response


# Normalize either a detailed LLM record or a simple task-ID-to-tests mapping.
def normalize_suite(response, task_id):
    payload = response.get(str(task_id), response.get(task_id, {}))
    if isinstance(payload, list):
        raise ValueError(
            f"AI response for task {task_id} must include tests, execution_results, covered_paths, and suite_correct"
        )
    if not isinstance(payload, dict):
        raise ValueError(f"LLM response for task {task_id} must be an object or test array")
    required_fields = {"tests", "execution_results", "covered_paths", "suite_correct"}
    missing_fields = required_fields - payload.keys()
    if missing_fields:
        raise ValueError(f"AI response for task {task_id} is missing: {', '.join(sorted(missing_fields))}")
    tests = payload.get("tests", payload.get("test_cases", []))
    if not isinstance(tests, list) or not all(isinstance(test, str) for test in tests):
        raise ValueError(f"LLM tests for task {task_id} must be an array of strings")
    execution_results = payload.get("execution_results", payload.get("test_results", []))
    covered_paths = payload.get("covered_paths", [])
    return {
        "tests": tests,
        "execution_results": execution_results if isinstance(execution_results, list) else [],
        "covered_paths": [str(path) for path in covered_paths] if isinstance(covered_paths, list) else [],
        "covered_paths_supplied": "covered_paths" in payload,
        "suite_correct": payload.get("suite_correct"),
        "property": payload.get("property"),
    }


# Calculate quality metrics from LLM-supplied execution and path-coverage evidence.
def evaluate_metrics(code, suite, objective, runs):
    path_data = identify_paths(code)
    execution_results = suite["execution_results"]
    passed_results = [result for result in execution_results if result.get("passed") is True]
    suite_correct = suite.get("suite_correct")
    if suite_correct is None and execution_results:
        suite_correct = len(passed_results) == len(execution_results)
    total_paths = path_data.get("total_paths", 0)
    evidence_supplied = bool(execution_results) and suite["covered_paths_supplied"]
    if not evidence_supplied:
        covered_paths = None
        path_coverage = None
        complete_paths = False
    elif path_data.get("branch_count", 0) == 0:
        covered_paths = 1
        path_coverage = 1.0
        complete_paths = True
    else:
        covered_paths = len(set(suite["covered_paths"]))
        path_coverage = round(covered_paths / total_paths, 4) if total_paths else None
        complete_paths = bool(total_paths and covered_paths >= total_paths)
    probability = None
    at_least_one = None
    if objective == "statistical":
        if suite_correct is not None and evidence_supplied:
            correct_runs = runs if suite_correct and complete_paths else 0
            probability = correct_runs / runs if runs else 0
            at_least_one = 1 - ((1 - probability) ** runs) if runs else 0
        else:
            correct_runs = None
    else:
        correct_runs = None
    return {
        "generated_test_count": len(suite["tests"]),
        "generated_tests_passed": len(passed_results),
        "generated_test_pass_rate": round(len(passed_results) / len(execution_results), 4) if execution_results else None,
        "execution_evidence_supplied": bool(execution_results),
        "path_evidence_supplied": suite["covered_paths_supplied"],
        "total_paths": total_paths,
        "covered_paths": covered_paths,
        "path_coverage": path_coverage,
        "complete_path_coverage": complete_paths,
        "branches": path_data.get("branches", []),
        "objective": objective,
        "statistical_runs": runs if objective == "statistical" else None,
        "statistical_correct_runs": correct_runs,
        "estimated_suite_correctness": probability,
        "estimated_at_least_one_correct_suite": at_least_one,
        "quality_verdict": "GOOD" if suite_correct and complete_paths else "NEEDS_REVIEW",
    }


# Create the prompt Copilot must use to generate and execute the requested suites.
def write_copilot_prompt(tasks, objective_instructions, request, output_path):
    sections = [
        "You are GitHub Copilot Chat acting as the MBPP test-generation and test-execution agent.",
        objective_instructions,
        TEST_PROMPT.replace("{objective_instructions}", objective_instructions).replace(
            "Task:\n{prompt}\n\nImplementation:\n{code}", ""
        ),
        f"User specification:\n{request}" if request else "",
        "For every task, create assert tests, execute them using the available tools, identify the path IDs covered, and return only valid JSON.",
        '{"task_id": {"tests": ["assert ..."], "execution_results": [{"index": 1, "passed": true}], "covered_paths": ["0", "1"], "suite_correct": true}}',
        "Do not omit task IDs. Do not return markdown or explanations.",
    ]
    for task in tasks:
        sections.extend([
            f"TASK_ID: {task['task_id']}",
            "IMPLEMENTATION:",
            task["code"],
            "EXISTING_TESTS:",
            json.dumps(task.get("test", task.get("test_list", []))),
            "TEST_SETUP_CODE:",
            task.get("test_setup_code", ""),
        ])
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n\n".join(section for section in sections if section), encoding="utf-8")


# Write the LLM-produced implementation, tests, prompt comment, evidence, and metrics.
def write_program_artifacts(program_dir, task, suite, metrics, chat_prompt):
    program_dir.mkdir(parents=True, exist_ok=True)
    implementation_source = "# MBPP implementation supplied as the unit under evaluation.\n\n" + task["code"]
    program_dir.joinpath("implementation.py").write_text(implementation_source, encoding="utf-8")
    program_dir.joinpath("generated_tests.json").write_text(json.dumps(suite["tests"], indent=2), encoding="utf-8")
    prompt_comment = "\n".join(
        ["# Copilot Chat prompt:"] + [f"# {line}" for line in chat_prompt.splitlines()]
    )
    test_source = (
        prompt_comment
        + "\n# Tests were generated and executed by the AI agent.\n\n"
        + "\n".join(suite["tests"])
        + "\n"
    )
    program_dir.joinpath("generated_tests.py").write_text(test_source, encoding="utf-8")
    program_dir.joinpath("test_results.json").write_text(json.dumps(suite["execution_results"], indent=2), encoding="utf-8")
    program_dir.joinpath("statistics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")


# Parse CLI input, generate Copilot prompts, or score LLM-produced evidence.
def main():
    parser = argparse.ArgumentParser(description="Metrics pipeline for LLM-created and LLM-executed MBPP tests")
    parser.add_argument("--dataset", default="data/mbpp_modified.jsonl")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--objective", choices=["auto", "coverage", "property", "statistical"], default="auto")
    parser.add_argument("--coverage-criterion", default="all branches")
    parser.add_argument("--property", default="The function returns the correct result for normal and edge inputs.")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--chat-prompt", default="")
    parser.add_argument("--copilot-prompt-output")
    parser.add_argument("--llm-response", help="JSON response created and executed by Copilot or another LLM")
    parser.add_argument("--output", default="results/mbpp_results.json")
    args = parser.parse_args()

    requested_ids = re.findall(r"\bprogram\s+(\d+)\b", args.chat_prompt.lower())
    tasks = load_tasks(args.dataset, None if requested_ids else args.limit)
    tasks = select_requested_tasks(tasks, args.chat_prompt, args.limit)
    objective = infer_objective(args.chat_prompt, args.objective)
    objective_instructions = {
        "coverage": f"Use the coverage objective: {args.coverage_criterion}.",
        "property": f"Use the property objective: {args.property}",
        "statistical": f"Use the statistical objective across {args.runs} runs.",
    }[objective]

    if args.copilot_prompt_output:
        write_copilot_prompt(tasks, objective_instructions, args.chat_prompt, args.copilot_prompt_output)
        print(f"Copilot prompt: {args.copilot_prompt_output}")
        print(f"Tasks included: {len(tasks)}")
        return
    if not args.llm_response:
        raise SystemExit("Provide --llm-response from Copilot Chat, or use --copilot-prompt-output first.")

    response = load_llm_response(args.llm_response)
    output = Path(args.output)
    programs_dir = output.parent / output.stem / "programs"
    report_tasks = []
    for task in tasks:
        suite = normalize_suite(response, task["task_id"])
        metrics = evaluate_metrics(task["code"], suite, objective, args.runs)
        metrics.update({"task_id": task["task_id"], "objective": objective})
        write_program_artifacts(programs_dir / f"program_{task['task_id']:03d}", task, suite, metrics, args.chat_prompt)
        report_tasks.append({"task_id": task["task_id"], "tests": suite["tests"], "quality": metrics})

    report = {
        "dataset": "MBPP",
        "dataset_path": args.dataset,
        "source": "llm_response",
        "settings": {"objective": objective, "coverage_criterion": args.coverage_criterion, "property": args.property, "runs": args.runs},
        "tasks": report_tasks,
        "summary": {
            "tasks": len(report_tasks),
            "quality_good": sum(task["quality"]["quality_verdict"] == "GOOD" for task in report_tasks),
            "quality_needs_review": sum(task["quality"]["quality_verdict"] == "NEEDS_REVIEW" for task in report_tasks),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"report: {output}")


if __name__ == "__main__":
    main()
