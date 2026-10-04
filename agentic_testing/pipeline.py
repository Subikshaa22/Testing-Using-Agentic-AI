"""Pipeline orchestration: a linear, single-pass flow (no agent framework).

For each MBPP task:
  1. Code generator agent writes solution.py.
  2. The generated code is checked against the dataset's reference asserts (ground truth).
  3. Test generator agent writes test_solution.py for the selected test requirement:
       coverage    - one suite that must reach a coverage criterion
       property    - one suite that checks user-specified properties P1..Pn
       statistical - n independent suites (different seeds) for statistical estimation
  4. Test executor agent runs each suite against the generated code (with coverage)
     and against the MBPP reference solution (oracle), then reports statistics
     and a verdict. Nothing is fed back to earlier agents.
"""
import json
import os
import shutil
import time
from dataclasses import replace

from .agents import CodeGeneratorAgent, TestGeneratorAgent, TestExecutorAgent, run_reference_tests
from .dataset import function_name, complexity, tier
from .llm import LLMError, DailyLimitError
from .stats import pass_at_k


class Pipeline:
    def __init__(self, config, llm, run_dir, properties=None, delay=0.0):
        self.config = config
        self.delay = delay  # pause between test-generation requests (provider rate limits)
        self.llm = llm
        self.run_dir = run_dir
        self.properties = properties or {}
        self.code_agent = CodeGeneratorAgent(llm, config.code_settings)
        self.test_agent = TestGeneratorAgent(llm, config.test_settings, config.mode,
                                             config.criterion, config.target)
        self.exec_agent = TestExecutorAgent(config.exec_timeout)

    def run_task(self, task, log=print):
        task_id = task["task_id"]
        task_dir = os.path.join(self.run_dir, f"task_{task_id}")
        os.makedirs(task_dir, exist_ok=True)
        if hasattr(self.llm, "current_task"):
            self.llm.current_task = task

        problem = task["text"]
        func = function_name(task)
        cc = complexity(task["code"])
        record = {"task_id": task_id, "mode": self.config.mode, "problem": problem, "function": func,
                  "complexity": cc, "tier": tier(cc)}
        if self.config.mode == "property":
            record["properties"] = self.properties[task_id]
            record["example"] = task["test_list"][0]

        # ---- Agent 1: code generation -------------------------------------- #
        log(f"[task {task_id}] ({tier(cc)}, CC={cc}) code generator: {problem[:60]}...")
        try:
            code = self.code_agent.run(problem, func, task["test_list"][0])
        except DailyLimitError:
            raise
        except LLMError as e:
            record.update(status="error", error=f"code generation failed: {e}")
            return self._save(task_dir, record)
        _write(os.path.join(task_dir, "solution.py"), code)

        ref = run_reference_tests(os.path.join(task_dir, "_ref"), code, task["test_list"],
                                  task.get("test_setup_code", ""), self.config.exec_timeout)
        shutil.rmtree(os.path.join(task_dir, "_ref"), ignore_errors=True)
        record["code_correctness"] = ref
        log(f"[task {task_id}]   generated code vs MBPP asserts: {ref['passed']}/{ref['total']}")

        reference_code = (task.get("test_setup_code") or "") + "\n" + task["code"] + "\n"

        # ---- Agents 2 + 3: generate tests, execute, report -------------------- #
        try:
            if self.config.mode == "statistical":
                self._statistical(task_id, task_dir, problem, code, reference_code, record, log)
            else:
                self._single_suite(task_id, task_dir, problem, code, reference_code, record, log)
        except DailyLimitError:
            raise
        except LLMError as e:
            record.update(status="error", error=f"test generation failed: {e}")
            return self._save(task_dir, record)

        record["status"] = "done"
        return self._save(task_dir, record)

    # ---- coverage / property: one suite ------------------------------------ #
    def _single_suite(self, task_id, task_dir, problem, code, reference_code, record, log):
        tests = self.test_agent.run(problem, code, record.get("properties"),
                                    func_name=record["function"], example=record.get("example"))
        suite = self._execute_suite(task_dir, code, reference_code, tests)
        record.update(suite)
        t = suite["tests"]
        v = suite["classification"]
        if self.config.mode == "coverage":
            verdict = self.exec_agent.coverage_verdict(
                {"tests": t, "coverage": suite["coverage"]}, self.config.criterion, self.config.target)
            c = suite["coverage"]
            log(f"[task {task_id}]   tests {t['passed']}/{t['total']} passed, stmt {c['statement']}%, "
                f"branch {c['branch']}%, loop {c['loop']}%, valid {v['valid_tests']}/{t['total']} "
                f"-> {verdict['verdict']}")
        else:
            verdict = self.exec_agent.property_verdict({"tests": t}, record["properties"])
            summary = ", ".join(f"{k}={p['verdict']}" for k, p in verdict["properties"].items())
            log(f"[task {task_id}]   tests {t['passed']}/{t['total']} passed; {summary} -> {verdict['verdict']}")
        record["verdict"] = verdict

    # ---- statistical: n independent suites --------------------------------- #
    def _statistical(self, task_id, task_dir, problem, code, reference_code, record, log):
        n = self.config.samples
        samples = []
        for i in range(1, n + 1):
            # A different seed per sample makes the n suites independent draws.
            settings = replace(self.config.test_settings, seed=self.config.test_settings.seed + i)
            if i > 1 and self.delay and not self.llm.is_cached(*self.test_agent.request(problem, code, settings)):
                time.sleep(self.delay)
            tests = self.test_agent.run(problem, code, settings=settings)
            suite = self._execute_suite(os.path.join(task_dir, f"sample_{i}"), code, reference_code, tests)
            suite["sample"] = i
            suite["correct"] = self.exec_agent.suite_correct(suite["tests_on_reference"])
            samples.append(suite)
            t = suite["tests"]
            log(f"[task {task_id}]   sample {i}/{n}: {t['passed']}/{t['total']} pass on generated code, "
                f"{suite['classification']['valid_tests']}/{t['total']} valid, "
                f"branch {suite['coverage']['branch']}% -> {'CORRECT' if suite['correct'] else 'INCORRECT'}")
        c = sum(s["correct"] for s in samples)
        record["samples"] = samples
        record["statistics"] = {
            "n": n,
            "correct_suites": c,
            "pass_at_k": {f"pass@{k}": round(pass_at_k(n, c, k), 4) for k in self.config.ks if k <= n},
            "mean_branch_coverage": round(sum(s["coverage"]["branch"] for s in samples) / n, 2),
            "mean_statement_coverage": round(sum(s["coverage"]["statement"] for s in samples) / n, 2),
            "mean_loop_coverage": round(sum(s["coverage"]["loop"] for s in samples) / n, 2),
            "mean_validity_rate": round(sum(s["classification"]["validity_rate"] for s in samples) / n, 2),
        }
        log(f"[task {task_id}]   {c}/{n} correct suites; " +
            ", ".join(f"{k}={v}" for k, v in record["statistics"]["pass_at_k"].items()))

    def _execute_suite(self, suite_dir, code, reference_code, tests):
        """Run one suite on the generated code (with coverage) and on the reference solution."""
        os.makedirs(suite_dir, exist_ok=True)
        _write(os.path.join(suite_dir, "test_solution.py"), tests)
        gen = self.exec_agent.execute(os.path.join(suite_dir, "run_generated"), code, tests)
        gen["coverage"].update(self.exec_agent.measure_loops(os.path.join(suite_dir, "run_loops"), code, tests))
        ref = self.exec_agent.execute(os.path.join(suite_dir, "run_reference"), reference_code, tests,
                                      measure_coverage=False)
        return {
            "tests": gen["tests"],
            "coverage": gen["coverage"],
            "tests_on_reference": {k: ref["tests"][k] for k in ("total", "passed", "failed", "collection_error")},
            "classification": self.exec_agent.classify(gen["tests"], ref["tests"]),
        }

    @staticmethod
    def _save(task_dir, record):
        with open(os.path.join(task_dir, "verdict.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        return record


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
