"""CSE731 mid-term project: agentic AI pipeline for unit test generation on MBPP.

Linear pipeline: code generator -> test case generator -> test case executor.
The test requirement is chosen with --mode (or interactively if omitted):

  1. coverage     achieve a user-specified coverage criterion (statement / branch / both)
  2. property     check user-specified properties of the function
  3. statistical  generate n suites per task and estimate P(at least one suite is correct) = pass@k

Examples
--------
  python main.py                                         # interactive menu
  python main.py --mode coverage --tasks 11 23 388 --criterion branch --target 100
  python main.py --mode property --properties properties_example.json
  python main.py --mode statistical --tasks 11 23 --samples 5 --k 1 3 5
  python main.py --mode coverage --provider mock --tasks 11 12   # offline, no API key
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

from agentic_testing.agents import CodeGeneratorAgent
from agentic_testing.config import (PipelineConfig, LLMSettings, MODES, PROVIDERS, RUNS_DIR, ROOT_DIR,
                                    DATA_DIR)
from agentic_testing.dataset import load_mbpp, select_stratified, complexity, tier, function_name
from agentic_testing.llm import (OpenAICompatibleClient, MockClient, DailyLimitError, load_dotenv,
                                 free_requests_remaining)
from agentic_testing.pipeline import Pipeline
from agentic_testing.prompts import CRITERIA_DESCRIPTIONS as CRITERIA
from agentic_testing.report import build_report


def parse_args():
    p = argparse.ArgumentParser(description="Agentic AI unit-test generation pipeline (MBPP).")
    p.add_argument("--mode", choices=MODES, help="test requirement; omit for an interactive menu")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--tasks", type=int, nargs="+", help="MBPP task ids to process (11-510 is the test split)")
    sel.add_argument("--sample", type=int, help="randomly sample N tasks from the MBPP test split (11-510)")
    sel.add_argument("--stratified", type=int, metavar="N",
                     help="pick N tasks spread evenly over complexity tiers (simple / medium / complex)")
    p.add_argument("--include", type=int, nargs="+", default=[],
                   help="with --stratified: task ids that must be part of the selection")
    p.add_argument("--seed", type=int, default=42, help="random seed for --sample and the LLM")
    g = p.add_argument_group("coverage mode")
    g.add_argument("--criterion", choices=list(CRITERIA), default=None,
                   help="statement, branch (decisions), loop (0/1/2+ iterations), "
                        "both (statement+branch) or all (statement+branch+loop); default branch")
    g.add_argument("--target", type=float, default=None, help="required coverage percentage (default 100)")
    g = p.add_argument_group("property mode")
    g.add_argument("--properties", metavar="FILE",
                   help='JSON file {"<task_id>": ["property 1", "property 2", ...]}')
    g.add_argument("--property", action="append", default=[], metavar="TEXT",
                   help="a property applied to every selected task (repeatable)")
    g = p.add_argument_group("statistical mode")
    g.add_argument("--samples", type=int, default=None, help="n: test suites generated per task (default 5)")
    g.add_argument("--k", type=int, nargs="+", default=None, help="k values for pass@k (default 1 3 5)")
    g = p.add_argument_group("model")
    g.add_argument("--provider", choices=list(PROVIDERS) + ["mock"], default=default_provider(),
                   help="openrouter, groq, custom (any OpenAI-compatible --base-url) or mock (offline); "
                        "default: $LLM_PROVIDER, else the provider of $LLM_BASE_URL, else openrouter")
    g.add_argument("--model", default=None, help="default: $LLM_MODEL (with $LLM_BASE_URL) or the provider's default")
    g.add_argument("--base-url", default=None, help="required for --provider custom (or $LLM_BASE_URL)")
    g.add_argument("--api-key", default=None,
                   help="default: $OPENROUTER_API_KEY / $GROQ_API_KEY for that provider, or $LLM_API_KEY "
                        "when $LLM_BASE_URL points at the chosen provider")
    g.add_argument("--code-temperature", type=float, default=0.2)
    g.add_argument("--test-temperature", type=float, default=None,
                   help="default 0.4 (coverage/property) or 0.8 (statistical)")
    p.add_argument("--delay", type=float, default=0.0, help="seconds to wait between tasks and between statistical samples (free-tier rate limits)")
    p.add_argument("--list-models", action="store_true", help="list OpenRouter's currently free models and exit")
    p.add_argument("--budget", type=int, default=None,
                   help="max API requests this run may send (default: OpenRouter's remaining free requests)")
    p.add_argument("--no-cache", action="store_true",
                   help="always call the API, even for a request already answered before")
    p.add_argument("--dry-run", action="store_true",
                   help="show the selected tasks and the request estimate, then exit without calling the API")
    p.add_argument("--resume", metavar="RUN_DIR",
                   help="continue an interrupted run in RUN_DIR, skipping tasks that already finished "
                        "(use the same options as the original run)")
    return p.parse_args()


# ---- provider resolution ----------------------------------------------------- #
def provider_of(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if host.endswith("openrouter.ai"):
        return "openrouter"
    if host.endswith("groq.com"):
        return "groq"
    return "custom"


def default_provider():
    if os.environ.get("LLM_PROVIDER"):
        return os.environ["LLM_PROVIDER"]
    if os.environ.get("LLM_BASE_URL"):
        return provider_of(os.environ["LLM_BASE_URL"])
    return "openrouter"


def resolve_endpoint(args):
    """Base URL, model and API key for the chosen provider.

    The generic $LLM_BASE_URL / $LLM_MODEL / $LLM_API_KEY settings are used only when
    $LLM_BASE_URL belongs to the chosen provider, so a key is never sent to another service.
    """
    preset = PROVIDERS[args.provider]
    env_url = os.environ.get("LLM_BASE_URL")
    paired = bool(env_url) and provider_of(env_url) == args.provider
    base_url = args.base_url or (env_url if paired else None) or preset["base_url"]
    model = args.model or (os.environ.get("LLM_MODEL") if paired else None) or preset["model"]
    api_key = (args.api_key or os.environ.get(preset["key_env"])
               or (os.environ.get("LLM_API_KEY") if paired else None))
    return base_url, model, api_key, preset["key_env"]


# ---- interactive helpers ----------------------------------------------------- #
def _input(prompt):
    try:
        return input(prompt)
    except EOFError:
        sys.exit("\nNo input available - pass the options on the command line instead (see --help).")


def ask(prompt, default=None, choices=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = _input(f"{prompt}{suffix}: ").strip() or (str(default) if default is not None else "")
        if not choices or answer in choices:
            return answer
        print(f"  please enter one of: {', '.join(choices)}")


def choose_mode():
    print("Select the test requirement for the test case generator agent:\n"
          "  1) Coverage    - achieve a coverage criterion (statement / branch / both)\n"
          "  2) Property    - check user-specified properties of the function\n"
          "  3) Statistical - estimate P(at least one generated suite is correct) over n samples")
    return MODES[int(ask("Choice", "1", ["1", "2", "3"])) - 1]


def ask_properties(task):
    print(f"\nTask {task['task_id']}: {task['text']}\n  example: {task['test_list'][0]}")
    print("Enter properties one per line (blank line to finish):")
    props = []
    while True:
        line = _input(f"  P{len(props) + 1}: ").strip()
        if not line:
            if props:
                return props
            print("  at least one property is required")
            continue
        props.append(line)


# ---- misc ------------------------------------------------------------------- #
def load_finished(run_dir, task_id, mode):
    path = os.path.join(run_dir, f"task_{task_id}", "verdict.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    return record if record.get("status") == "done" and record.get("mode") == mode else None


def list_free_models():
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=30) as resp:
        models = json.loads(resp.read().decode("utf-8"))["data"]
    for m in sorted(models, key=lambda m: m["id"]):
        if m["id"].endswith(":free"):
            print(m["id"])


def main():
    load_dotenv(os.path.join(ROOT_DIR, ".env"))
    args = parse_args()
    if args.list_models:
        return list_free_models()

    # Without --mode, the user is asked for every setting not given as a flag.
    interactive = args.mode is None
    mode = args.mode or choose_mode()

    # ---- mode-specific parameters (flags first, then prompt, then default) ---- #
    criterion, target, samples, ks = args.criterion, args.target, args.samples, args.k
    if mode == "coverage":
        if criterion is None:
            criterion = ask("Coverage criterion (" + "/".join(CRITERIA) + ")", "branch",
                            list(CRITERIA)) if interactive else "branch"
        if target is None:
            target = float(ask("Target coverage %", "100")) if interactive else 100.0
    if mode == "statistical":
        if samples is None:
            samples = int(ask("Samples per task (n)", "5")) if interactive else 5
        if ks is None:
            ks = [k for k in (1, 3, 5) if k <= samples]
        if any(k > samples for k in ks):
            sys.exit(f"every k must be <= n ({samples})")

    tasks = load_mbpp()
    file_props = {}
    if args.properties:
        with open(args.properties, encoding="utf-8") as f:
            file_props = {int(k): v for k, v in json.load(f).items() if not k.startswith("_")}

    if args.tasks:
        missing = [t for t in args.tasks if t not in tasks]
        if missing:
            sys.exit(f"Unknown MBPP task ids: {missing}")
        selected = [tasks[t] for t in args.tasks]
    elif args.stratified:
        selected = [tasks[t] for t in select_stratified(tasks, args.stratified, args.seed, args.include)]
    elif mode == "property" and file_props and not args.sample:
        selected = [tasks[t] for t in file_props]
    else:
        pool = [tasks[t] for t in range(11, 511)]
        selected = random.Random(args.seed).sample(pool, args.sample or 5)

    properties = {}
    if mode == "property":
        for task in selected:
            tid = task["task_id"]
            properties[tid] = file_props.get(tid) or args.property or None
            if not properties[tid]:
                properties[tid] = ask_properties(task)

    test_temp = args.test_temperature
    if test_temp is None:
        test_temp = 0.8 if mode == "statistical" else 0.4
    base_url, model, api_key, key_env = (resolve_endpoint(args) if args.provider != "mock"
                                         else ("-", "mock", None, None))
    if args.provider != "mock" and not (base_url and model):
        sys.exit("--provider custom needs --base-url and --model (or $LLM_BASE_URL / $LLM_MODEL)")
    config = PipelineConfig(
        model=model if args.provider != "mock" else "mock",
        base_url=base_url if args.provider != "mock" else "-",
        provider=args.provider,
        mode=mode,
        criterion=criterion or "branch",
        target=target if target is not None else 100.0,
        samples=samples or 5,
        ks=tuple(ks or (1, 3, 5)),
        code_settings=LLMSettings(temperature=args.code_temperature, seed=args.seed),
        test_settings=LLMSettings(temperature=test_temp, max_tokens=6000, seed=args.seed),
    )

    # ---- LLM client (with response cache and request budget) ------------------ #
    cache_dir = None if args.no_cache else os.path.join(DATA_DIR, "llm_cache")
    remaining = None
    if args.provider == "mock":
        llm = MockClient()
    else:
        if not api_key:
            sys.exit(f"No API key for --provider {args.provider}. Set {key_env} in .env (see .env.example) "
                     "or pass --api-key, or use --provider mock for an offline demo.")
        remaining = free_requests_remaining(config.base_url, api_key)
        budget = args.budget if args.budget is not None else remaining
        llm = OpenAICompatibleClient(config.model, config.base_url, api_key,
                                     cache_dir=cache_dir, budget=budget)

    # ---- request estimate: code generation may already be cached --------------- #
    pending = [t for t in selected
               if not (args.resume and load_finished(os.path.abspath(args.resume), t["task_id"], mode))]
    code_needed = sum(not llm.is_cached(CodeGeneratorAgent.messages(t["text"], function_name(t), t["test_list"][0]),
                                        config.code_settings) for t in pending)
    tests_needed = len(pending) * (config.samples if mode == "statistical" else 1)
    detail = {"coverage": f"criterion {config.criterion} >= {config.target:.0f}%",
              "property": "user-specified properties",
              "statistical": f"n={config.samples}, k={list(config.ks)}"}[mode]
    print(f"\nModel: {config.model} | mode: {mode} ({detail})")
    print("Tasks:")
    for t in selected:
        cc = complexity(t["code"])
        print(f"  {t['task_id']:>4}  {tier(cc):<8} CC={cc:<3} {t['text'][:70]}")
    print(f"API requests needed: ~{code_needed + tests_needed} "
          f"({code_needed} code generation [{len(pending) - code_needed} cached], {tests_needed} test generation; "
          f"retries extra)")
    if remaining is not None:
        print(f"Free requests remaining today: {remaining}")
    if args.dry_run:
        return

    if args.resume:
        run_dir = os.path.abspath(args.resume)
        if not os.path.isdir(run_dir):
            sys.exit(f"--resume: no such run directory {run_dir}")
    else:
        run_dir = os.path.join(RUNS_DIR, time.strftime(f"run_%Y%m%d_%H%M%S_{mode}"))
    os.makedirs(run_dir, exist_ok=True)
    llm.log_path = os.path.join(run_dir, "prompts.jsonl")
    print(f"Run directory: {run_dir}\n")

    pipeline = Pipeline(config, llm, run_dir, properties, delay=args.delay)
    records = []
    for i, task in enumerate(selected):
        previous = load_finished(run_dir, task["task_id"], mode) if args.resume else None
        if previous:
            print(f"[task {task['task_id']}] already done in this run directory - skipped")
            records.append(previous)
            continue
        if i and args.delay:
            time.sleep(args.delay)
        try:
            records.append(pipeline.run_task(task))
        except KeyboardInterrupt:
            print("\nInterrupted - writing report for the tasks finished so far.")
            break
        except DailyLimitError as e:
            argv = sys.argv[:]
            if "--resume" in argv:
                del argv[argv.index("--resume"):argv.index("--resume") + 2]
            if "--mode" not in argv:
                argv += ["--mode", mode]
            print(f"\n[task {task['task_id']}] {e}\nStopping. Continue after the reset with:\n"
                  f"  python {' '.join(argv)} --resume {run_dir}")
            break
        except Exception as e:  # one bad task must not kill the whole run
            print(f"[task {task['task_id']}]   ERROR: {type(e).__name__}: {e}")
            records.append(pipeline._save(os.path.join(run_dir, f"task_{task['task_id']}"), {
                "task_id": task["task_id"], "mode": mode, "problem": task["text"], "status": "error",
                "error": f"{type(e).__name__}: {e}"}))
        # Rewrite the report after every task so partial results are never lost.
        build_report(run_dir, config, records)

    summary = build_report(run_dir, config, records)
    print("\n=== Summary ===")
    for k, v in summary.items():
        print(f"  {k:45s} {v}")
    print(f"\nAPI requests sent: {getattr(llm, 'requests_sent', 0)} | answered from cache: {llm.cache_hits}")
    print(f"Report: {os.path.join(run_dir, 'report.md')}")


if __name__ == "__main__":
    main()
