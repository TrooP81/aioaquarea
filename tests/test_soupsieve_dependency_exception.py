"""Static regression checks for the closed SoupSieve dependency exception."""

from __future__ import annotations

import ast
from pathlib import Path
import re

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_FILE = PROJECT_ROOT / ".github" / "workflows" / "optimizer-ci.yml"
ROOT_MANIFEST_FILE = PROJECT_ROOT / "pyproject.toml"
OPTIMIZER_MANIFEST_FILE = PROJECT_ROOT / "heatpump-optimizer" / "pyproject.toml"
EXCEPTION_FILE = (
    PROJECT_ROOT
    / ".github"
    / "dependency-exceptions"
    / "DEP-EXC-2026-09-18-SOUPSIEVE.md"
)
FOLLOW_UP_FILE = PROJECT_ROOT / ".github" / "dependency-followups.md"
APPLICATION_PYTHON_TREES = (
    PROJECT_ROOT / "aioaquarea",
    PROJECT_ROOT / "heatpump-optimizer" / "packages",
)
SOUPSIEVE_SELECTOR_METHODS = {"select", "select_one", "css"}


def _find_soupsieve_selector_calls(source: str, filename: str) -> list[str]:
    module = ast.parse(source, filename=filename)
    beautiful_soup_factories = set()
    beautiful_soup_modules = set()
    soupsieve_receivers = set()
    selector_calls = []

    def assigned_names(target: ast.AST) -> list[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.List, ast.Tuple)):
            names = []
            for element in target.elts:
                names.extend(assigned_names(element))
            return names
        return []

    def is_beautiful_soup_factory(call: ast.AST) -> bool:
        if not isinstance(call, ast.Call):
            return False
        if isinstance(call.func, ast.Name):
            return call.func.id in beautiful_soup_factories
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "BeautifulSoup"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in beautiful_soup_modules
        )

    def is_soupsieve_receiver(expression: ast.AST) -> bool:
        if isinstance(expression, ast.Name):
            return expression.id in soupsieve_receivers
        if isinstance(expression, ast.Attribute):
            return is_soupsieve_receiver(expression.value)
        if isinstance(expression, ast.Call):
            return is_beautiful_soup_factory(expression) or (
                isinstance(expression.func, ast.Attribute)
                and is_soupsieve_receiver(expression.func.value)
            )
        return False

    class ReachabilityVisitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name == "bs4":
                    beautiful_soup_modules.add(alias.asname or "bs4")
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module == "bs4":
                for alias in node.names:
                    if alias.name == "BeautifulSoup":
                        beautiful_soup_factories.add(alias.asname or alias.name)
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            if is_soupsieve_receiver(node.value):
                for target in node.targets:
                    soupsieve_receivers.update(assigned_names(target))
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None and is_soupsieve_receiver(node.value):
                soupsieve_receivers.update(assigned_names(node.target))
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in SOUPSIEVE_SELECTOR_METHODS
                and is_soupsieve_receiver(node.func.value)
            ):
                selector_calls.append(f"{filename}:{node.lineno}")
            self.generic_visit(node)

    ReachabilityVisitor().visit(module)
    return selector_calls


def test_soupsieve_exception_is_retained_as_closed_history() -> None:
    exception = EXCEPTION_FILE.read_text(encoding="utf-8")

    for required_value in (
        "DEP-EXC-2026-09-18-SOUPSIEVE",
        "Remediated on 2026-09-22",
        "2026-09-18",
        "Repository maintainer (Carlos J. Aliaga)",
        "soupsieve` 2.8.4",
        "soupsieve>=2.9,<3",
        "must not be reused as an audit suppression",
    ):
        assert required_value in exception


def test_supply_chain_audits_have_no_soupsieve_suppression() -> None:
    workflow = WORKFLOW_FILE.read_text(encoding="utf-8")
    library_workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "library-checks.yml"
    ).read_text(encoding="utf-8")

    assert "- run: python -m pip_audit" in workflow
    assert "- run: python -m pip_audit" in library_workflow
    assert "--ignore-vuln" not in workflow
    assert "--ignore-vuln" not in library_workflow
    assert "continue-on-error" not in workflow


def test_soupsieve_dependency_floors_close_the_exception() -> None:
    root_manifest = ROOT_MANIFEST_FILE.read_text(encoding="utf-8")
    optimizer_manifest = OPTIMIZER_MANIFEST_FILE.read_text(encoding="utf-8")
    constraints = (PROJECT_ROOT / "heatpump-optimizer" / "constraints.txt").read_text(
        encoding="utf-8"
    )
    follow_up = FOLLOW_UP_FILE.read_text(encoding="utf-8")

    assert '"soupsieve>=2.9,<3"' in root_manifest
    assert '"aioaquarea @ git+' in optimizer_manifest
    assert "soupsieve==2.9.2" in constraints
    assert "Status: Closed 2026-09-22" in follow_up


def test_application_code_has_no_soupsieve_css_selector_calls() -> None:
    selector_calls = []
    for source_tree in APPLICATION_PYTHON_TREES:
        for source_file in source_tree.rglob("*.py"):
            selector_calls.extend(
                _find_soupsieve_selector_calls(
                    source_file.read_text(encoding="utf-8"),
                    str(source_file.relative_to(PROJECT_ROOT)),
                )
            )

    assert selector_calls == []


def test_soupsieve_reachability_scan_covers_selector_apis_without_false_positives() -> (
    None
):
    source = """
import bs4 as bs
from bs4 import BeautifulSoup as Soup
from sqlalchemy import select

document = bs.BeautifulSoup("<p></p>", "html.parser")
document.select("p")
document.select_one("p")
document.css("p")

soup = Soup("<p></p>", "html.parser")
soup.find("p").select("span")
select(object())
unrelated.select()
"""

    assert _find_soupsieve_selector_calls(source, "synthetic.py") == [
        "synthetic.py:7",
        "synthetic.py:8",
        "synthetic.py:9",
        "synthetic.py:12",
    ]
