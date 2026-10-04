"""The three agents of the pipeline (linear: no feedback between agents).

CodeGeneratorAgent  -> problem text                         -> solution.py
TestGeneratorAgent  -> problem + code + test requirement    -> test_solution.py
TestExecutorAgent   -> code + tests (+ reference solution)  -> execution results, statistics, verdict
"""
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from . import loopcov, prompts
from .llm import extract_code_block


def number_lines(code):
    return "\n".join(f"{i:>3}: {line}" for i, line in enumerate(code.splitlines(), 1))


# --------------------------------------------------------------------------- #
class CodeGeneratorAgent:
    name = "code_generator"

    def __init__(self, llm, settings):
        self.llm = llm
        self.settings = settings

    @staticmethod
    def messages(problem, func_name, example):
        return [
            {"role": "system", "content": prompts.CODE_GEN_SYSTEM},
            {"role": "user", "content": prompts.CODE_GEN_USER.format(
                problem=problem, func_name=func_name, example=example)},
        ]

    def run(self, problem, func_name, example):
        messages = self.messages(problem, func_name, example)
        return extract_code_block(self.llm.chat(self.name, messages, self.settings))


# --------------------------------------------------------------------------- #
class TestGeneratorAgent:
    name = "test_generator"

    def __init__(self, llm, settings, mode, criterion="branch", target=100.0):
        self.llm = llm
        self.settings = settings
        self.mode = mode
        self.criterion = criterion
        self.target = target

    def system_prompt(self):
        if self.mode == "coverage":
            return prompts.TEST_GEN_SYSTEM_COVERAGE.format(
                criterion_text=prompts.CRITERIA_DESCRIPTIONS[self.criterion], target=self.target)
        if self.mode == "property":
            return prompts.TEST_GEN_SYSTEM_PROPERTY
        return prompts.TEST_GEN_SYSTEM_STATISTICAL

    def run(self, problem, code, properties=None, settings=None, func_name=None, example=None):
        messages, settings = self.request(problem, code, settings, properties, func_name, example)
        return extract_code_block(self.llm.chat(self.name, messages, settings))

    def request(self, problem, code, settings=None, properties=None, func_name=None, example=None):
        """The (messages, settings) pair that run() sends - also used to check the response cache."""
        if self.mode == "property":
            # Black-box: specification, signature example and properties only - never the code.
            user = prompts.TEST_GEN_USER_BLACKBOX.format(problem=problem, func_name=func_name, example=example)
            user += prompts.TEST_GEN_USER_PROPERTIES.format(
                properties="\n".join(f"P{i}: {p}" for i, p in enumerate(properties, 1)))
        else:
            # White-box: coverage and statistical modes see the numbered code.
            user = prompts.TEST_GEN_USER.format(problem=problem, numbered_code=number_lines(code))
        messages = [{"role": "system", "content": self.system_prompt()},
                    {"role": "user", "content": user}]
        return messages, settings or self.settings


# --------------------------------------------------------------------------- #
class TestExecutorAgent:
    """Runs a generated test suite and turns the results into statistics and a verdict.

    The suite is executed twice:
      * against the generated code, under coverage.py (--branch);
      * against the MBPP reference solution, which acts as a trusted oracle and
        tells us whether each test's expectation is actually correct.
    Comparing the two runs classifies every test without needing an LLM:
      pass/pass -> OK, fail(gen)/pass(ref) -> CODE_BUG, fail/fail -> TEST_BUG,
      pass(gen)/fail(ref) -> SUSPECT_TEST (the test agrees with the generated
      code but not with the reference).
    """
    name = "test_executor"

    def __init__(self, timeout=30):
        self.timeout = timeout

    # ---- execution -------------------------------------------------------- #
    def execute(self, workdir, code, tests, measure_coverage=True):
        os.makedirs(workdir, exist_ok=True)
        _write(os.path.join(workdir, "solution.py"), code)
        _write(os.path.join(workdir, "test_solution.py"), tests)
        for stale in ("junit.xml", "coverage.json", ".coverage"):
            p = os.path.join(workdir, stale)
            if os.path.exists(p):
                os.remove(p)

        pytest_args = ["-m", "pytest", "test_solution.py", "-q", "-p", "no:cacheprovider",
                       "--rootdir", ".", "--junitxml=junit.xml", "--tb=short"]
        if measure_coverage:
            cmd = [sys.executable, "-m", "coverage", "run", "--branch", "--include=solution.py"] + pytest_args
        else:
            cmd = [sys.executable] + pytest_args
        timed_out = False
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=self.timeout)
            output = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as e:
            timed_out = True
            output = f"TIMEOUT after {self.timeout}s\n{e.stdout or ''}"
        _write(os.path.join(workdir, "pytest_output.txt"), output)

        result = self._parse_junit(os.path.join(workdir, "junit.xml"))
        result["timed_out"] = timed_out
        result["collection_error"] = timed_out or result["total"] == 0 or any(
            c["name"] in ("test_solution", "") for c in result["cases"] if c["status"] == "failed")
        execution = {"tests": result}
        if measure_coverage:
            execution["coverage"] = self._measure_coverage(workdir)
        return execution

    @staticmethod
    def _parse_junit(path):
        result = {"total": 0, "passed": 0, "failed": 0, "cases": []}
        if not os.path.exists(path):
            return result
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            skipped = case.find("skipped") is not None
            status = "skipped" if skipped else ("failed" if problem is not None else "passed")
            entry = {"name": case.get("name"), "status": status}
            if problem is not None:
                entry["message"] = (problem.get("message") or "")[:300]
                entry["details"] = (problem.text or "")[-1200:]
            result["cases"].append(entry)
            result["total"] += 1
            result["passed"] += status == "passed"
            result["failed"] += status == "failed"
        return result

    def _measure_coverage(self, workdir):
        cov = {"statement": 0.0, "branch": 0.0, "num_statements": 0, "covered_statements": 0,
               "num_branches": 0, "covered_branches": 0, "missing_lines": [], "missing_branches": []}
        try:
            subprocess.run([sys.executable, "-m", "coverage", "json", "-o", "coverage.json"],
                           cwd=workdir, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return cov
        path = os.path.join(workdir, "coverage.json")
        if not os.path.exists(path):
            return cov
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        file_data = next((v for k, v in data.get("files", {}).items()
                          if os.path.basename(k) == "solution.py"), None)
        if not file_data:
            return cov
        s = file_data["summary"]
        cov["num_statements"] = s.get("num_statements", 0)
        cov["covered_statements"] = s.get("covered_lines", 0)
        cov["num_branches"] = s.get("num_branches", 0)
        cov["covered_branches"] = s.get("covered_branches", 0)
        cov["statement"] = round(100.0 * cov["covered_statements"] / cov["num_statements"], 2) \
            if cov["num_statements"] else 100.0
        # No decision points at all -> branch coverage is vacuously 100%; flagged separately.
        cov["branch"] = round(100.0 * cov["covered_branches"] / cov["num_branches"], 2) \
            if cov["num_branches"] else 100.0
        cov["missing_lines"] = file_data.get("missing_lines", [])
        cov["missing_branches"] = file_data.get("missing_branches", [])
        return cov

    def measure_loops(self, workdir, code, tests):
        """Loop coverage: run the suite on a copy of the code whose loops count their iterations."""
        try:
            instrumented, loops = loopcov.instrument(code)
        except SyntaxError:
            return loopcov.summarize([], {})
        if not loops:
            return loopcov.summarize([], {})
        os.makedirs(workdir, exist_ok=True)
        _write(os.path.join(workdir, "solution.py"), instrumented)
        _write(os.path.join(workdir, "_loopcov.py"), loopcov.RUNTIME)
        _write(os.path.join(workdir, "test_solution.py"), tests)
        result = os.path.join(workdir, "loopcov.json")
        if os.path.exists(result):
            os.remove(result)
        try:
            subprocess.run([sys.executable, "-m", "pytest", "test_solution.py", "-q", "-p", "no:cacheprovider",
                            "--rootdir", "."], cwd=workdir, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            pass
        seen = {}
        if os.path.exists(result):
            with open(result, encoding="utf-8") as f:
                seen = json.load(f)
        return loopcov.summarize(loops, seen)

    # ---- statistics shared by all modes ------------------------------------ #
    @staticmethod
    def classify(gen_tests, ref_tests):
        """Label each test by comparing its outcome on generated vs reference code."""
        ref_status = {c["name"]: c["status"] for c in ref_tests["cases"]}
        labels, counts = {}, {"OK": 0, "CODE_BUG": 0, "TEST_BUG": 0, "SUSPECT_TEST": 0}
        for case in gen_tests["cases"]:
            if case["status"] == "skipped":
                continue
            g = case["status"] == "passed"
            r = ref_status.get(case["name"]) == "passed"
            label = "OK" if g and r else "CODE_BUG" if r else "TEST_BUG" if not g else "SUSPECT_TEST"
            labels[case["name"]] = label
            counts[label] += 1
        valid = sum(1 for c in ref_tests["cases"] if c["status"] == "passed")
        return {
            "labels": labels,
            "counts": counts,
            # A test is "valid" when its expectation holds for the trusted reference solution.
            "valid_tests": valid,
            "validity_rate": round(100.0 * valid / ref_tests["total"], 2) if ref_tests["total"] else 0.0,
        }

    # ---- verdicts ---------------------------------------------------------- #
    @staticmethod
    def criterion_score(coverage, criterion):
        if criterion == "statement":
            return coverage["statement"]
        if criterion == "branch":
            return coverage["branch"]
        if criterion == "loop":
            return coverage["loop"]
        if criterion == "both":
            return min(coverage["statement"], coverage["branch"])
        return min(coverage["statement"], coverage["branch"], coverage["loop"])  # "all"

    def coverage_verdict(self, execution, criterion, target):
        t, c = execution["tests"], execution["coverage"]
        reasons = []
        if t["collection_error"]:
            reasons.append("test file could not be collected/executed")
        if t["failed"]:
            reasons.append(f"{t['failed']}/{t['total']} tests failed on the generated code")
        score = self.criterion_score(c, criterion)
        if score < target:
            reasons.append(f"{criterion} coverage {score:.1f}% < target {target:.0f}%")
        return {"verdict": "PASS" if not reasons else "FAIL", "criterion": criterion,
                "criterion_coverage": score, "target": target,
                "vacuous_branch_coverage": c["num_branches"] == 0,
                "vacuous_loop_coverage": c["num_loops"] == 0, "reasons": reasons}

    @staticmethod
    def property_verdict(execution, properties):
        """Map tests named test_p<k>_... to property P<k> and judge each property."""
        per_prop = {i: {"property": p, "tests": [], "passed": 0, "failed": 0}
                    for i, p in enumerate(properties, 1)}
        unmapped = []
        for case in execution["tests"]["cases"]:
            m = re.match(r"test_p(\d+)_", case["name"] or "")
            k = int(m.group(1)) if m else None
            if k not in per_prop:
                unmapped.append(case["name"])
                continue
            per_prop[k]["tests"].append(case["name"])
            per_prop[k]["passed"] += case["status"] == "passed"
            per_prop[k]["failed"] += case["status"] == "failed"
        for entry in per_prop.values():
            entry["verdict"] = ("UNTESTED" if not entry["tests"] else
                                "VIOLATED" if entry["failed"] else "SATISFIED")
        verdicts = [e["verdict"] for e in per_prop.values()]
        overall = ("PASS" if all(v == "SATISFIED" for v in verdicts) else
                   "FAIL" if "VIOLATED" in verdicts else "INCOMPLETE")
        return {"verdict": overall, "properties": {f"P{k}": v for k, v in per_prop.items()},
                "unmapped_tests": unmapped}

    @staticmethod
    def suite_correct(ref_tests):
        """Statistical mode: a suite is correct iff it runs and every test holds on the reference."""
        return (not ref_tests["collection_error"] and ref_tests["total"] > 0
                and ref_tests["failed"] == 0)


# --------------------------------------------------------------------------- #
def run_reference_tests(workdir, code, test_list, setup_code="", timeout=30):
    """Ground-truth check: run the MBPP dataset asserts against the generated code.

    This is independent of the generated tests and tells us whether the code
    generator produced a correct implementation.
    """
    os.makedirs(workdir, exist_ok=True)
    script = [setup_code or "", "from solution import *", "import math", "results = []"]
    for assertion in test_list:
        script.append(f"try:\n    {assertion.strip()}\n    results.append(True)\n"
                      f"except Exception:\n    results.append(False)")
    script.append("import json; print('__RESULTS__' + json.dumps(results))")
    path = os.path.join(workdir, "_reference_check.py")
    _write(os.path.join(workdir, "solution.py"), code)
    _write(path, "\n".join(script) + "\n")
    try:
        proc = subprocess.run([sys.executable, "_reference_check.py"], cwd=workdir,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"passed": 0, "total": len(test_list), "error": "timeout"}
    finally:
        os.remove(path)
    match = re.search(r"__RESULTS__(\[.*\])", proc.stdout)
    if not match:
        return {"passed": 0, "total": len(test_list), "error": proc.stderr[-800:]}
    results = json.loads(match.group(1))
    return {"passed": sum(results), "total": len(results), "per_assert": results}


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
