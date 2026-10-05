"""Emit selector evidence for this suite when the official runner requests it."""

from __future__ import annotations

import ast
from copy import copy
import inspect
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest


_SUITE = Path(__file__).resolve().parent
_manifest = []
_reports = defaultdict(list)


def pytest_sessionstart(session):
    """Keep separate pytest sessions from sharing collected items or outcomes."""
    _manifest.clear()
    _reports.clear()


def _symbols(text):
    """Use the same class/function qualification as the evidence AST checker."""
    found = {}

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                found[name] = child
                walk(child, name + ".")

    walk(ast.parse(text))
    return found


def pytest_collection_finish(session):
    """Bind only real collected suite items, including their exact parameter IDs."""
    if not os.environ.get("AI_TEST_AGENT_RESULT"):
        return
    root = Path(os.environ.get("AI_TEST_AGENT_ROOT", session.config.rootpath)).resolve()
    parsed = {}
    for item in session.items:
        path = Path(item.path).resolve()
        if _SUITE not in path.parents:
            continue
        if path not in parsed:
            parsed[path] = _symbols(path.read_text(encoding="utf-8"))
        symbol = getattr(inspect.unwrap(item.obj), "__qualname__", "")
        node = parsed[path].get(symbol)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise pytest.UsageError(
                f"Cannot bind collected item {item.nodeid} to a Python symbol in {path}"
            )
        _manifest.append(
            {"selector": item.nodeid, "file": path.relative_to(root).as_posix(), "symbol": symbol}
        )


def pytest_runtest_logreport(report):
    """Keep phase outcomes; process exit alone never establishes a Case pass."""
    if os.environ.get("AI_TEST_AGENT_RESULT"):
        _reports[report.nodeid].append(copy(report))


def _result(selector):
    reports = _reports.get(selector, [])
    phases = {report.when: report for report in reports}
    unstable = len(phases) != len(reports) or any(
        report.outcome not in {"passed", "failed", "skipped"}
        or (report.passed and bool(getattr(report, "wasxfail", False)))
        for report in reports
    )
    if any(report.failed for report in phases.values()):
        status = "failed"
    elif any(report.skipped for report in phases.values()):
        status = "skipped"
    elif set(phases) == {"setup", "call", "teardown"} and all(
        report.passed for report in phases.values()
    ):
        status = "passed"
    else:
        status = "not_run"
    return {"selector": selector, "status": status, "unstable": unstable}


def pytest_sessionfinish(session, exitstatus):
    """Write an atomic v2 report, including zero collection and unrun items."""
    requested = os.environ.get("AI_TEST_AGENT_RESULT")
    if not requested:
        return
    selectors = [binding["selector"] for binding in _manifest]
    counts = {selector: selectors.count(selector) for selector in set(selectors)}
    results = [_result(selector) for selector in dict.fromkeys(selectors)]
    for result in results:
        result["unstable"] = result["unstable"] or counts[result["selector"]] != 1
    payload = {
        "schema_version": "2.0",
        "collected": len(_manifest),
        "results": results,
        "manifest": _manifest,
    }
    destination = Path(requested)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=destination.name + ".", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
