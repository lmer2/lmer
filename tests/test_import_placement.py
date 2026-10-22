"""Every deferred import says why it is deferred — issue #544 item 1.

An import inside a function runs on that function's first call, not at module
load. That is the right choice for exactly three reasons, and each one is
easy to get wrong in the other direction:

* ``cycle`` — hoisting it raises ``ImportError`` from a partially initialised
  module, or would on some import order;
* ``import cost`` — hoisting it puts a measurable cost on an entry point that
  does not need the module (a web stack on ``lmer-signal``, say);
* ``optional dependency`` — the module may not be installed at all.

Anything else belongs at the top of the module, where a broken import fails
at load instead of on whichever call path happens to reach it first.

The rule, kept deliberately small:

* An import inside a function needs ``# deferred: <reason> — <why>`` on its
  own first line, or as a full-line comment directly above it. ``<reason>``
  is one of the three words above; ``<why>`` names the cycle or the cost.
* An unbroken run of import lines in the same block shares the comment on
  its first import: each following import must start on the line straight
  after the previous one ends. A blank line, a comment line or any other
  statement ends the run.
* Imports that run at module load — top level, module-level ``if``/``try``
  blocks, class bodies — are not deferred and need nothing. Neither do
  ``if TYPE_CHECKING:`` bodies, which never run.

How to judge a case, and the measurements behind the existing comments:
docs/DEVELOPMENT.md, "Deferred imports".
"""
import ast
import io
from pathlib import Path
import re
import tokenize

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

REASONS = ("cycle", "import cost", "optional dependency")
_MARK = re.compile(r"# deferred:")
_VALID = re.compile(r"# deferred: (?:%s) — \S" % "|".join(map(re.escape, REASONS)))


def _comments(source):
    """Line number -> (comment text, whether the comment is the whole line)."""
    found = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            row, col = token.start
            found[row] = (token.string, not token.line[:col].strip())
    return found


def _is_type_checking(test):
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def violations(source, name="<string>"):
    """Each deferred import in ``source`` that breaks the rule, as a message."""
    lines = source.splitlines()
    comments = _comments(source)
    problems = []

    def marker(lineno):
        own = comments.get(lineno)
        if own and _MARK.search(own[0]):
            return own[0]
        above = comments.get(lineno - 1)
        if above and above[1] and _MARK.search(above[0]):
            return above[0]
        return None

    def judge(stmt, run_end):
        where = f"{name}:{stmt.lineno}: {lines[stmt.lineno - 1].strip()}"
        text = marker(stmt.lineno)
        if text is not None:
            if _VALID.search(text):
                return True
            problems.append(
                f"{where} — the reason must be one of {', '.join(REASONS)}, "
                "followed by ' — <why>'")
            return False
        if run_end == stmt.lineno - 1:
            return True
        problems.append(
            f"{where} — a deferred import needs '# deferred: <reason> — <why>' "
            "on its line or the line above, or hoisting to the module top")
        return False

    def block(statements, in_function):
        run_end = None
        for stmt in statements:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                if in_function:
                    run_end = stmt.end_lineno if judge(stmt, run_end) else None
                continue
            run_end = None
            if isinstance(stmt, ast.If) and _is_type_checking(stmt.test):
                block(stmt.orelse, in_function)
                continue
            visit(stmt, in_function or isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef)))

    def visit(node, in_function):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if isinstance(statements, list):
                block(statements, in_function)
        for child in [*getattr(node, "handlers", ()), *getattr(node, "cases", ())]:
            visit(child, in_function)

    visit(ast.parse(source), False)
    return problems


def test_every_deferred_import_under_src_says_why():
    problems = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        problems += violations(path.read_text(encoding="utf-8"), rel)
    assert not problems, (
        f"{len(problems)} function-local import(s) without a valid deferral "
        "reason (see docs/DEVELOPMENT.md, 'Deferred imports'):\n"
        + "\n".join(problems))


def check(source):
    return violations(source.lstrip("\n"))


def test_a_comment_on_the_same_line_passes():
    assert check("""
def f():
    import json  # deferred: import cost — measured +60 ms on the entry
""") == []


def test_a_full_line_comment_directly_above_passes():
    assert check("""
def f():
    # deferred: cycle — json imports this module back
    import json
""") == []


def test_an_import_without_a_comment_fails():
    [problem] = check("""
def f():
    import json
""")
    assert "<string>:2: import json" in problem
    assert "# deferred: <reason> — <why>" in problem


def test_an_unknown_reason_is_its_own_error():
    [problem] = check("""
def f():
    import json  # deferred: habit — it was always here
""")
    assert "the reason must be one of cycle, import cost, optional dependency" in problem


def test_a_reason_without_a_why_fails():
    [problem] = check("""
def f():
    import json  # deferred: cycle
""")
    assert "the reason must be one of" in problem


def test_a_trailing_comment_on_the_statement_above_does_not_count():
    [problem] = check("""
def f():
    x = 1  # deferred: cycle — not about the import below
    import json
""")
    assert "<string>:3:" in problem


def test_an_unbroken_run_shares_the_first_imports_comment():
    assert check("""
def f():
    # deferred: optional dependency — the matrix extra
    from mautrix.client import Client
    from mautrix.types import (
        RoomID,
    )
    import mautrix.errors
""") == []


def test_a_blank_line_ends_the_run():
    [problem] = check("""
def f():
    import json  # deferred: cycle — json imports this module back

    import csv
""")
    assert "<string>:4: import csv" in problem


def test_a_comment_line_ends_the_run():
    [problem] = check("""
def f():
    import json  # deferred: cycle — json imports this module back
    # a remark
    import csv
""")
    assert "<string>:4: import csv" in problem


def test_another_statement_ends_the_run():
    [problem] = check("""
def f():
    import json  # deferred: cycle — json imports this module back
    x = 1
    import csv
""")
    assert "<string>:4: import csv" in problem


def test_a_failing_import_does_not_start_a_run():
    problems = check("""
def f():
    import json
    import csv
""")
    assert [p.split(" — ")[0] for p in problems] == [
        "<string>:2: import json", "<string>:3: import csv"]


def test_imports_in_nested_blocks_and_methods_are_checked():
    problems = check("""
class C:
    import json

    def m(self):
        try:
            import csv
        except ImportError:
            import pickle
""")
    assert [p.split(" — ")[0] for p in problems] == [
        "<string>:6: import csv", "<string>:8: import pickle"]


def test_imports_that_run_at_module_load_need_nothing():
    assert check("""
import json
try:
    import yaml
except ImportError:
    yaml = None
if json:
    import csv
""") == []


def test_type_checking_bodies_never_run_and_need_nothing():
    assert check("""
from typing import TYPE_CHECKING
def f():
    if TYPE_CHECKING:
        import json
""") == []
