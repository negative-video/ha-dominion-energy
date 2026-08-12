"""Tests for the integration's translation files.

Home Assistant only loads UI strings for custom integrations from
``translations/<lang>.json``; ``strings.json`` is consumed by core's build
pipeline and is never read at runtime. These tests make sure the runtime file
exists, stays in sync with ``strings.json``, and covers every step, error and
abort reason the config flow can actually produce.

The expected keys are derived from ``config_flow.py`` with ``ast`` so the tests
keep working as the flow evolves.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "dominion_energy"
STRINGS_PATH = COMPONENT_DIR / "strings.json"
TRANSLATIONS_PATH = COMPONENT_DIR / "translations" / "en.json"
CONFIG_FLOW_PATH = COMPONENT_DIR / "config_flow.py"

CONFIG_FLOW_CLASS = "DominionEnergyConfigFlow"
OPTIONS_FLOW_CLASS = "DominionEnergyOptionsFlow"


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _class_node(name: str) -> ast.ClassDef:
    """Return the AST node for a class defined in config_flow.py."""
    tree = ast.parse(CONFIG_FLOW_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"Class {name} not found in {CONFIG_FLOW_PATH}")


def _keyword_string(node: ast.Call, name: str) -> str | None:
    """Return the constant string passed as ``name`` to a call, if any."""
    for keyword in node.keywords:
        if (
            keyword.arg == name
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None


def _method_calls(class_node: ast.ClassDef, method: str) -> list[ast.Call]:
    """Return all ``self.<method>(...)`` style calls made inside a class."""
    return [
        node
        for node in ast.walk(class_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
    ]


def _step_ids(class_name: str) -> set[str]:
    """Collect the step IDs shown by a flow class.

    Menus count: ``async_show_menu`` renders a step like any other and reads
    the same ``title``/``description`` keys, it just lists links instead of
    fields.
    """
    class_node = _class_node(class_name)
    return {
        step_id
        for method in ("async_show_form", "async_show_menu")
        for call in _method_calls(class_node, method)
        if (step_id := _keyword_string(call, "step_id")) is not None
    }


def _menu_options(class_name: str) -> dict[str, list[str]]:
    """Collect each menu step's list of destinations."""
    class_node = _class_node(class_name)
    menus: dict[str, list[str]] = {}
    for call in _method_calls(class_node, "async_show_menu"):
        step_id = _keyword_string(call, "step_id")
        if step_id is None:
            continue
        for keyword in call.keywords:
            if keyword.arg != "menu_options" or not isinstance(keyword.value, ast.List):
                continue
            menus[step_id] = [
                element.value
                for element in keyword.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
    return menus


def _abort_reasons(class_name: str) -> set[str]:
    """Collect the literal abort reasons raised by a flow class."""
    class_node = _class_node(class_name)
    return {
        reason
        for call in _method_calls(class_node, "async_abort")
        if (reason := _keyword_string(call, "reason")) is not None
    }


def _error_keys(class_name: str) -> set[str]:
    """Collect every error key a flow class can put in front of the user.

    Two forms count, and both end up in ``errors["base"]``:

    * ``errors["base"] = "..."`` — shown by the step that sets it.
    * ``self._carried_error = "..."`` — set by a step that then delegates to an
      earlier one, which picks it up via ``_take_carried_error()``. A step's
      own ``errors`` dict does not survive delegation, so this is how a reason
      crosses the hop.

    Missing the second form would leave its strings unguarded: the key would
    still reach the UI, but nothing would notice if the translation went away.
    """
    class_node = _class_node(class_name)
    keys: set[str] = set()
    for node in ast.walk(class_node):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            shown_directly = (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "errors"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "base"
            )
            carried_over = (
                isinstance(target, ast.Attribute)
                and target.attr == "_carried_error"
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            )
            if shown_directly or carried_over:
                keys.add(node.value.value)
    return keys


CONFIG_STEPS = _step_ids(CONFIG_FLOW_CLASS)
OPTIONS_STEPS = _step_ids(OPTIONS_FLOW_CLASS)
OPTIONS_MENUS = _menu_options(OPTIONS_FLOW_CLASS)
CONFIG_ERRORS = _error_keys(CONFIG_FLOW_CLASS)
CONFIG_ABORTS = _abort_reasons(CONFIG_FLOW_CLASS)

#: Every (menu step, destination) pair, flattened for parametrisation.
OPTIONS_MENU_ENTRIES = sorted(
    (step_id, option)
    for step_id, options in OPTIONS_MENUS.items()
    for option in options
)


class TestOptionsAreNotClobbered:
    """An options flow's ``async_create_entry`` replaces ``entry.options``.

    Writing only the keys one step collected therefore silently deletes every
    option set by a different step. The flow routes all of its writes through
    one merging helper so that cannot happen; these checks are what stop a new
    step from quietly reintroducing the bug.
    """

    def _options_flow_source(self) -> str:
        return ast.unparse(_class_node(OPTIONS_FLOW_CLASS))

    def test_the_merge_helper_exists_and_merges(self) -> None:
        source = self._options_flow_source()
        assert "_save" in source
        assert "self._config_entry.options" in source, (
            "the save helper must fold new values into the existing options"
        )

    def test_no_step_calls_async_create_entry_directly(self) -> None:
        """Every write goes through the helper, so every write merges."""
        class_node = _class_node(OPTIONS_FLOW_CLASS)
        direct = [
            node
            for node in _method_calls(class_node, "async_create_entry")
            # The helper itself is the one legitimate caller.
            if not _inside_function(class_node, node, "_save")
        ]
        assert not direct, (
            "an options step calls async_create_entry directly; it will "
            "discard every option collected by the other steps"
        )


def _inside_function(class_node: ast.ClassDef, target: ast.AST, name: str) -> bool:
    """Report whether ``target`` sits inside the named method of a class."""
    for node in class_node.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return any(child is target for child in ast.walk(node))
    return False


def test_translations_file_exists() -> None:
    """Home Assistant reads translations/en.json, not strings.json."""
    assert TRANSLATIONS_PATH.is_file(), (
        f"{TRANSLATIONS_PATH} is missing - without it Home Assistant falls back "
        "to raw translation keys in the setup UI"
    )


def test_translations_match_strings() -> None:
    """translations/en.json must stay identical in content to strings.json."""
    assert _load_json(TRANSLATIONS_PATH) == _load_json(STRINGS_PATH), (
        "strings.json and translations/en.json have drifted apart; "
        "copy strings.json over translations/en.json"
    )


def test_ast_extraction_found_something() -> None:
    """Guard against the AST helpers silently returning nothing."""
    assert CONFIG_STEPS
    assert OPTIONS_STEPS
    assert CONFIG_ERRORS


@pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
@pytest.mark.parametrize("step_id", sorted(CONFIG_STEPS))
def test_config_steps_translated(path: Path, step_id: str) -> None:
    """Every config flow step shown by the flow has translated strings."""
    steps = _load_json(path)["config"]["step"]
    assert step_id in steps, f"{path.name} is missing config.step.{step_id}"
    assert steps[step_id].get("title"), (
        f"{path.name}: config.step.{step_id} has no title"
    )


@pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
@pytest.mark.parametrize("step_id", sorted(OPTIONS_STEPS))
def test_options_steps_translated(path: Path, step_id: str) -> None:
    """Every options flow step shown by the flow has translated strings."""
    steps = _load_json(path)["options"]["step"]
    assert step_id in steps, f"{path.name} is missing options.step.{step_id}"
    assert steps[step_id].get("title"), (
        f"{path.name}: options.step.{step_id} has no title"
    )


@pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
@pytest.mark.parametrize(("step_id", "option"), OPTIONS_MENU_ENTRIES)
def test_options_menu_options_translated(path: Path, step_id: str, option: str) -> None:
    """A menu entry with no label renders as its raw key."""
    step = _load_json(path)["options"]["step"][step_id]
    labels = step.get("menu_options", {})
    assert option in labels, (
        f"{path.name} is missing options.step.{step_id}.menu_options.{option}"
    )
    assert labels[option], f"{path.name}: {step_id}.menu_options.{option} is blank"


@pytest.mark.parametrize(("step_id", "option"), OPTIONS_MENU_ENTRIES)
def test_options_menu_leads_somewhere(step_id: str, option: str) -> None:
    """Every menu destination must be a step the flow can actually show."""
    assert option in OPTIONS_STEPS, (
        f"options.step.{step_id} links to {option!r}, but no "
        f"async_step_{option} shows a form"
    )


@pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
@pytest.mark.parametrize("error_key", sorted(CONFIG_ERRORS))
def test_config_errors_translated(path: Path, error_key: str) -> None:
    """Every errors["base"] value set by the flow has a translated message."""
    errors = _load_json(path)["config"]["error"]
    assert error_key in errors, f"{path.name} is missing config.error.{error_key}"


@pytest.mark.parametrize("path", [STRINGS_PATH, TRANSLATIONS_PATH])
@pytest.mark.parametrize("reason", sorted(CONFIG_ABORTS))
def test_config_aborts_translated(path: Path, reason: str) -> None:
    """Every literal async_abort reason has a translated message."""
    aborts = _load_json(path)["config"]["abort"]
    assert reason in aborts, f"{path.name} is missing config.abort.{reason}"
