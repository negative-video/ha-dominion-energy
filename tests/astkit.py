"""Source-inspection helpers shared by the Home-Assistant-free tests.

Several modules in this integration import Home Assistant at the top and so
cannot be imported by the lightweight CI job. What they contain, though, is
largely declarative -- entity descriptions, a unique-ID scheme, a device
identifier -- and those are exactly the contracts that break silently and are
impossible to notice without a live Home Assistant.

So they are read with ``ast`` instead. This module holds the parsing plumbing;
the assertions live with the tests that make them.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def module(path: Path) -> ast.Module:
    """Parse a source file."""
    return ast.parse(path.read_text(encoding="utf-8"))


def dotted(node: ast.expr) -> str:
    """Render a Name/Attribute chain as dotted source text."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    raise AssertionError(f"not a dotted name: {ast.dump(node)}")


def literal(node: ast.expr) -> Any:
    """Render a keyword value as a comparable Python value.

    Constants come through as themselves; enum members and module constants
    come through as their dotted source text (``"SensorDeviceClass.ENERGY"``),
    which is enough to compare against without importing Home Assistant.
    """
    if isinstance(node, ast.Constant):
        return node.value
    return dotted(node)


def format_template(node: ast.expr) -> str:
    """Render a string expression as a ``str.format`` template.

    ``f"{a}_{b}"`` becomes ``"{a}_{b}"`` and a bare ``a`` becomes ``"{a}"``, so
    an f-string and the variable it is built from can be compared and filled in
    by the tests. An interpolation that is not a plain dotted name is rendered
    as ``«source»`` - still inspectable, but deliberately not a ``str.format``
    field, since there is nothing sensible to substitute.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(format_template(value) for value in node.values)
    if isinstance(node, ast.FormattedValue):
        try:
            return "{" + dotted(node.value) + "}"
        except AssertionError:
            return f"«{ast.unparse(node.value)}»"
    return "{" + dotted(node) + "}"


def function(
    path: Path, name: str, class_name: str | None = None
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return a module-level or method function definition by name."""
    scope: list[ast.stmt] = module(path).body
    if class_name is not None:
        for node in scope:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                scope = node.body
                break
        else:
            raise AssertionError(f"class {class_name} not found in {path.name}")
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == name
        ):
            return node
    raise AssertionError(f"function {name} not found in {path.name}")


def assigned_templates(scope: ast.AST, target_name: str) -> list[str]:
    """Return every value assigned to ``target_name``, as format templates."""
    return [
        format_template(node.value)
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == target_name
            for target in node.targets
        )
    ]


def attribute_templates(scope: ast.AST, attribute: str) -> list[str]:
    """Return every value assigned to ``self.<attribute>``, as templates."""
    return [
        format_template(node.value)
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == attribute
            for target in node.targets
        )
    ]


def tuple_of_calls(path: Path, name: str, call_name: str) -> list[ast.Call]:
    """Return the ``call_name(...)`` calls making up a module-level tuple."""
    for node in module(path).body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        if target != name or node.value is None:
            continue
        assert isinstance(node.value, ast.Tuple), f"{name} is not a tuple literal"
        return [
            element
            for element in node.value.elts
            if isinstance(element, ast.Call)
            and isinstance(element.func, ast.Name)
            and element.func.id == call_name
        ]
    raise AssertionError(f"{name} not found in {path.name}")


def describe(call: ast.Call, skip: tuple[str, ...] = ("value_fn",)) -> dict[str, Any]:
    """Return a description call's keyword arguments as plain values."""
    fields: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in skip:
            continue
        fields[keyword.arg] = literal(keyword.value)
    return fields


class Key:
    """Stand-in exposing ``.key`` so ``{description.key}`` can be formatted."""

    def __init__(self, key: str) -> None:
        self.key = key
