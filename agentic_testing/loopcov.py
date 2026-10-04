"""Loop coverage: did the tests make every loop run 0 times, exactly once, and 2+ times?

coverage.py's branch coverage only tells us that a loop was entered and that it was
exited/skipped - it cannot tell 1 iteration from 5. Here every `for`/`while` statement
of the code under test is instrumented (via its AST) with counters:

    _lc.start(id)
    try:
        for x in xs:
            _lc.tick(id)        # one tick per iteration (also counts `continue`d ones)
            ...
    finally:
        _lc.end(id)             # records 0 / 1 / 2+ for this execution of the loop

The `finally` makes `break`, `return` and exceptions count too, and a per-loop stack
keeps recursive calls separate. Comprehensions are expressions, not loop statements,
and are not measured.
"""
import ast

CASES = ("0", "1", "2+")

# Runtime helper written next to the instrumented solution.py; dumps results at exit.
RUNTIME = '''import atexit, json

_stack, _seen = {}, {}


def start(i):
    _stack.setdefault(i, []).append(0)


def tick(i):
    _stack[i][-1] += 1


def end(i):
    n = _stack[i].pop()
    _seen.setdefault(i, set()).add("0" if n == 0 else "1" if n == 1 else "2+")


@atexit.register
def _dump():
    with open("loopcov.json", "w") as f:
        json.dump({str(k): sorted(v) for k, v in _seen.items()}, f)
'''


class _Instrument(ast.NodeTransformer):
    def __init__(self):
        self.loops = []  # loop id -> line number in the original code

    def _loop(self, node):
        self.generic_visit(node)  # instrument nested loops first
        lid = len(self.loops)
        self.loops.append(node.lineno)
        node.body.insert(0, ast.parse(f"_lc.tick({lid})").body[0])
        start = ast.parse(f"_lc.start({lid})").body[0]
        end = ast.parse(f"_lc.end({lid})").body[0]
        return [start, ast.Try(body=[node], handlers=[], orelse=[], finalbody=[end])]

    visit_For = visit_While = visit_AsyncFor = _loop


def instrument(code):
    """Return (instrumented source, [(loop id, line, header text)]); raises SyntaxError."""
    tree = ast.parse(code)
    inst = _Instrument()
    tree = inst.visit(tree)
    # Import the helper after any docstring / `from __future__` imports.
    pos = 0
    while pos < len(tree.body) and (
            isinstance(tree.body[pos], ast.ImportFrom) and tree.body[pos].module == "__future__"
            or pos == 0 and isinstance(tree.body[0], ast.Expr)
            and isinstance(getattr(tree.body[0], "value", None), ast.Constant)):
        pos += 1
    tree.body.insert(pos, ast.parse("import _loopcov as _lc").body[0])
    ast.fix_missing_locations(tree)
    lines = code.splitlines()
    loops = [(i, ln, lines[ln - 1].strip() if 0 < ln <= len(lines) else "?")
             for i, ln in enumerate(inst.loops)]
    return ast.unparse(tree) + "\n", loops


def summarize(loops, seen):
    """Loop coverage numbers from the instrumented run's results ({loop id: [cases]})."""
    covered, missing = 0, []
    for lid, line, src in loops:
        hit = set(seen.get(str(lid), []))
        covered += len(hit & set(CASES))
        lacking = [c for c in CASES if c not in hit]
        if lacking:
            missing.append({"line": line, "source": src, "missing": lacking})
    total = 3 * len(loops)
    return {
        # No loops at all -> loop coverage is vacuously 100%; flagged separately in the report.
        "loop": round(100.0 * covered / total, 2) if total else 100.0,
        "num_loops": len(loops),
        "covered_loop_cases": covered,
        "num_loop_cases": total,
        "missing_loop_cases": missing,
    }
