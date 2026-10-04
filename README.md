# Agentic AI Pipeline for Unit Test Generation

CSE731 Software Testing - Mid-term Project (Term I, 2026-27)

A linear three-agent pipeline, built from first principles in plain Python (no LangChain/LangGraph, no RAG,
no chain-of-thought prompting). It generates code for MBPP problems, generates unit tests for a
user-selected test requirement, executes them and reports statistics and a verdict.

## Pipeline

```
MBPP task ─► [1] Code Generator ─► solution.py ─► check against MBPP asserts (is the code correct?)
                                       │
                                       ▼
             [2] Test Case Generator ─► test_solution.py      (requirement chosen by the user)
                                       │
                                       ▼
             [3] Test Case Executor ─► run tests on generated code (pytest + coverage.py --branch)
                                     ─► run tests on MBPP reference solution (trusted oracle)
                                     ─► statistics + verdict
```

Each task goes through the agents once; nothing is fed back to an earlier agent.

## The three test requirements (`--mode`)

| Mode | What the test generator is asked to do | Verdict / statistics |
|---|---|---|
| `coverage` | Reach a coverage criterion (`--criterion statement \| branch \| loop \| both \| all`, `--target %`) | PASS if every test passes and coverage ≥ target; statement and branch coverage, uncovered lines/branches |
| `property` | Test each user-specified property P1..Pn (tests named `test_p<k>_...`). Black-box: the test generator sees the specification, function name, one example call and the properties, never the code | Per property: SATISFIED (all its tests pass) / VIOLATED (a test fails) / UNTESTED (no test); task PASS if all are satisfied |
| `statistical` | Generate `n` independent suites per task (`--samples`, temperature 0.8, seed + i) | Suite is correct if every test holds on the reference solution; c/n, unbiased **pass@k** = 1 − C(n−c,k)/C(n,k), 95% Wilson interval for per-suite correctness |

**Coverage criteria.** Statement and branch coverage come from coverage.py (`--branch`). Loop coverage
comes from `agentic_testing/loopcov.py`, which adds iteration counters to every `for`/`while` loop of the
generated code (via its AST) and reruns the suite. A loop is fully covered when the tests make it run
**0 times, exactly once and 2+ times**; loop coverage % = covered (loop, case) pairs / (3 × loops).
Comprehensions are not counted.

| `--criterion` | Required |
|---|---|
| `statement` | every statement executed |
| `branch` (default) | every decision takes both outcomes (if/elif/else, while, for entered/skipped) |
| `loop` | every loop runs 0, 1 and 2+ times |
| `both` | statement + branch |
| `all` | statement + branch + loop |

All three coverages are measured and reported in every mode; the criterion decides what the test
generator is asked for and what the coverage verdict checks.

**Test outcome classification (all modes).** Every test runs on both the generated code and the MBPP
reference solution:

| Generated code | Reference | Label | Meaning |
|---|---|---|---|
| pass | pass | OK | correct test, correct code |
| fail | pass | CODE_BUG | the test found a real bug in the generated code |
| fail | fail | TEST_BUG | the test's expected value is wrong |
| pass | fail | SUSPECT_TEST | the test agrees with the generated code but not with the reference |

*Test validity rate* = tests that pass on the reference / all tests.

## Agents and settings

| Agent | Input | Output | Settings |
|---|---|---|---|
| Code generator | problem text, function name, one example assert | `solution.py` | temperature 0.2, top_p 1.0, max_tokens 4000, seed 42 |
| Test case generator | problem text, numbered code, requirement (+ properties) | `test_solution.py` (pytest) | temperature 0.4 (0.8 statistical), top_p 1.0, max_tokens 6000, seed 42 (+i per sample) |
| Test case executor | code, tests, reference solution | test results, coverage, classification, verdict | deterministic (no LLM), 30 s timeout |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then put your OpenRouter key in .env
python main.py --list-models  # see which free models are currently available
```

## Running

```bash
# Interactive: choose the mode and its settings from a menu
python main.py --tasks 11 23 388

# 1) Coverage
python main.py --mode coverage --sample 10 --criterion branch --target 100 --delay 5

# 2) Property (properties from a file, or typed in when prompted)
python main.py --mode property --properties properties_example.json
python main.py --mode property --tasks 492 --property "Returns True if the item is in the list" \
                                           --property "Returns False if the item is not in the list"

# 3) Statistical
python main.py --mode statistical --tasks 11 23 388 --samples 5 --k 1 3 5

# Offline demo with no API key (MBPP reference code and asserts stand in for the LLM)
python main.py --mode coverage --provider mock --tasks 11 12 17
```

LLM requests per task: 2 for coverage and property, 1 + n for statistical. If a run stops at a daily
limit, it prints the `--resume <run_dir>` command that continues it.

## Providers (`--provider`)

| Provider | Key in `.env` | Default model | Free tier |
|---|---|---|---|
| `openrouter` (default) | `OPENROUTER_API_KEY` | `qwen/qwen3.8-27b:free` | ~50 requests/day (shown before each run) |
| `groq` | `GROQ_API_KEY` | `qwen/qwen3.8-27b` | ~1,000 requests/day per model, 30/min, limited tokens/day |
| `custom` | `LLM_API_KEY` | `--model` | any OpenAI-compatible `--base-url` (KiloAI, local Ollama, ...) |
| `mock` | none | - | offline; MBPP reference code and asserts stand in for the LLM |

Each provider receives only its own key. The model's built-in reasoning is switched off where the provider
allows it (no chain-of-thought). LLM responses are cached in `data/llm_cache/`, keyed by provider, model,
prompt and sampling settings, so repeating a request (another mode on the same tasks, a re-run) costs nothing.

```bash
python main.py --provider groq --mode coverage --stratified 15     # 5 simple, 5 medium, 5 complex tasks
python main.py --provider groq --mode statistical --tasks 151 388 492 31 --samples 10 --dry-run
```

## Outputs (`runs/run_<timestamp>_<mode>/`)

| File | Content |
|---|---|
| `report.md` | configuration, aggregate statistics, per-task tables, prompt templates |
| `summary.json` | machine-readable version of the whole run |
| `prompts.jsonl` | every LLM request: agent, model, settings, full messages, response, latency |
| `task_<id>/solution.py` | generated code |
| `task_<id>/test_solution.py` | generated test suite (`sample_<i>/test_solution.py` in statistical mode) |
| `task_<id>/verdict.json` | test results, coverage, classification, verdict / statistics |
| `.../run_generated/`, `.../run_reference/` | executed files, `junit.xml`, `coverage.json`, `pytest_output.txt` |

## Code layout

```
main.py                      CLI entry point, mode menu, task selection
properties_example.json      sample user-specified properties (tasks 11, 68, 280, 388, 492)
agentic_testing/
  config.py                  model and pipeline settings
  llm.py                     stdlib HTTP client for OpenAI-compatible APIs, mock client, logging
  dataset.py                 MBPP download and loading
  prompts.py                 all system and user prompt templates
  agents.py                  CodeGeneratorAgent, TestGeneratorAgent, TestExecutorAgent
  pipeline.py                linear orchestration for the three modes
  stats.py                   pass@k and Wilson confidence interval
  loopcov.py                 loop coverage: AST instrumentation + 0/1/2+ iteration counting
  report.py                  report.md / summary.json generation
```
