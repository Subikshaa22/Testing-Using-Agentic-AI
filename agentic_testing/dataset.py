"""MBPP (Mostly Basic Python Problems) loader.

The dataset is downloaded once from the google-research repository and cached
under data/mbpp.jsonl. Each record has: task_id, text (problem statement),
code (reference solution), test_list (3 asserts), challenge_test_list.
"""
import ast
import json
import os
import re
import urllib.request

from .config import DATA_DIR, MBPP_PATH, MBPP_URL


def ensure_dataset():
    if os.path.exists(MBPP_PATH):
        return MBPP_PATH
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"Downloading MBPP dataset from {MBPP_URL} ...")
    urllib.request.urlretrieve(MBPP_URL, MBPP_PATH)
    return MBPP_PATH


def load_mbpp():
    path = ensure_dataset()
    tasks = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rec = json.loads(line)
                tasks[rec["task_id"]] = rec
    return tasks


TIERS = ("simple", "medium", "complex")


def complexity(code):
    """McCabe cyclomatic complexity of the MBPP reference solution (1 + decision points)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    cc = 1
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.IfExp, ast.ExceptHandler)):
            cc += 1
        elif isinstance(node, ast.BoolOp):          # each extra `and`/`or` operand
            cc += len(node.values) - 1
        elif isinstance(node, ast.comprehension):   # loop + its `if` filters
            cc += 1 + len(node.ifs)
    return cc


def tier(cc):
    """simple: CC 1-2, medium: CC 3-4, complex: CC >= 5."""
    if cc is None or cc <= 2:
        return "simple"
    return "medium" if cc <= 4 else "complex"


def select_stratified(tasks, n, seed, include=()):
    """Pick n tasks from the test split (11-510), spread evenly over the complexity tiers.

    Tasks listed in `include` are always selected and count towards their tier.
    """
    import random
    rng = random.Random(seed)
    pools = {t: [] for t in TIERS}
    for tid in range(11, 511):
        if tid not in include:
            pools[tier(complexity(tasks[tid]["code"]))].append(tid)
    for pool in pools.values():
        rng.shuffle(pool)
    quota = {t: n // len(TIERS) + (i < n % len(TIERS)) for i, t in enumerate(TIERS)}
    chosen = list(include)
    for tid in include:
        quota[tier(complexity(tasks[tid]["code"]))] -= 1
    for t in TIERS:
        chosen += pools[t][:max(0, quota[t])]
    # Order by complexity so the report reads from simple to complex.
    return sorted(chosen, key=lambda tid: (complexity(tasks[tid]["code"]) or 0, tid))


def function_name(task):
    """Infer the function under test from the first dataset assert."""
    match = re.search(r"assert\s+\(?\s*(?:math\.isclose\(\s*|set\(\s*)?([A-Za-z_]\w*)\s*\(", task["test_list"][0])
    if match:
        return match.group(1)
    match = re.search(r"def\s+([A-Za-z_]\w*)\s*\(", task["code"])
    return match.group(1) if match else None
