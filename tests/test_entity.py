"""Tests for the shared entity identity.

``entity.py`` imports Home Assistant, so like ``test_sensor.py`` this module
reads it with ``ast`` rather than importing it.

Everything here guards the same thing: a changed unique ID orphans an entity
and discards its recorded history, and a changed device identifier scatters an
entry's entities across duplicate devices. Both are silent in normal use and
only noticed once the history is already gone.
"""

from __future__ import annotations

import ast

import pytest

from tests.astkit import Key, assigned_templates, attribute_templates, function
from tests.conftest import COMPONENT_DIR

ENTITY_PATH = COMPONENT_DIR / "entity.py"

ENTITY_BASE_CLASS = "DominionEnergyEntity"

# The unique-ID prefix every install used before config entries became unique
# per account *and* meter. An entry that already owns entities under this
# scheme must keep it, or its history is thrown away.
LEGACY_UNIQUE_ID_TEMPLATE = "{account_number}_{key}"
LEGACY_DEVICE_NAME_TEMPLATE = "Dominion Energy {account_number}"

ACCOUNT = "123456789123"
METER = "000000000296117800"
OTHER_METER = "000000000296117801"
KEY = "daily_usage"


def _resolve_identity() -> ast.FunctionDef | ast.AsyncFunctionDef:
    return function(ENTITY_PATH, "resolve_identity")


def _entity_template() -> str:
    """The single f-string every entity's unique ID is built from."""
    init = function(ENTITY_PATH, "__init__", ENTITY_BASE_CLASS)
    assignments = attribute_templates(init, "_attr_unique_id")
    assert len(assignments) == 1, "expected exactly one unique_id assignment"
    return assignments[0]


def _prefix_templates() -> list[str]:
    return assigned_templates(_resolve_identity(), "unique_id_prefix")


def _unique_ids() -> list[str]:
    """Render the full unique_id for each identity branch."""
    entity_template = _entity_template()
    rendered = []
    for prefix_template in _prefix_templates():
        prefix = prefix_template.format(account_number=ACCOUNT, meter_number=METER)
        rendered.append(
            entity_template.format(identity=_Identity(prefix), description=Key(KEY))
        )
    return rendered


class _Identity:
    """Stand-in exposing ``.unique_id_prefix`` for template rendering."""

    def __init__(self, unique_id_prefix: str) -> None:
        self.unique_id_prefix = unique_id_prefix


class TestUniqueIdScheme:
    """A changed unique_id orphans the entity and discards its history."""

    def test_entity_builds_its_id_from_a_prefix_and_the_key(self) -> None:
        assert _entity_template() == "{identity.unique_id_prefix}_{description.key}"

    def test_there_are_exactly_two_identity_branches(self) -> None:
        assert len(_prefix_templates()) == 2, (
            "expected a legacy branch and a meter-scoped branch"
        )

    def test_legacy_identity_is_byte_identical_to_the_old_scheme(self) -> None:
        """An existing single-meter entry keeps the ID it already registered."""
        expected = LEGACY_UNIQUE_ID_TEMPLATE.format(account_number=ACCOUNT, key=KEY)
        assert expected in _unique_ids(), (
            f"no identity branch still produces {expected!r}; every existing "
            "install would lose its entity history"
        )

    def test_second_meter_gets_a_distinct_identity(self) -> None:
        ids = _unique_ids()
        assert len(set(ids)) == 2, f"identity branches collide: {ids}"

    def test_meter_scoped_ids_differ_between_meters(self) -> None:
        meter_template = next(
            template for template in _prefix_templates() if "meter_number" in template
        )
        first = meter_template.format(account_number=ACCOUNT, meter_number=METER)
        second = meter_template.format(account_number=ACCOUNT, meter_number=OTHER_METER)
        assert first != second

    def test_every_platform_shares_one_id_builder(self) -> None:
        """Two platforms building IDs two ways is how they drift apart.

        The base class owns the f-string; a platform that assigned its own
        ``_attr_unique_id`` would silently opt out of the scheme above.
        """
        source = ENTITY_PATH.read_text(encoding="utf-8")
        assert source.count("_attr_unique_id") == 1

        for platform in ("sensor.py", "binary_sensor.py"):
            path = COMPONENT_DIR / platform
            if not path.is_file():
                continue
            assert "_attr_unique_id" not in path.read_text(encoding="utf-8"), (
                f"{platform} builds its own unique_id instead of using "
                f"{ENTITY_BASE_CLASS}"
            )


class TestDeviceIdentity:
    """The device is keyed the same way, for the same reason."""

    def _device_identifiers(self) -> list[str]:
        return assigned_templates(_resolve_identity(), "device_identifier")

    def _device_names(self) -> list[str]:
        return assigned_templates(_resolve_identity(), "device_name")

    def test_legacy_device_identifier_is_the_bare_account_number(self) -> None:
        rendered = [
            template.format(
                account_number=ACCOUNT,
                meter_number=METER,
                unique_id_prefix=f"{ACCOUNT}_{METER}",
            )
            for template in self._device_identifiers()
        ]
        assert ACCOUNT in rendered, (
            "an existing entry's device must keep identifiers={(DOMAIN, account)}"
        )
        assert len(set(rendered)) == 2, f"device identifiers collide: {rendered}"

    def test_legacy_device_name_is_unchanged(self) -> None:
        rendered = [
            template.format(account_number=ACCOUNT)
            for template in self._device_names()
            if "meter_number" not in template
        ]
        expected = LEGACY_DEVICE_NAME_TEMPLATE.format(account_number=ACCOUNT)
        assert rendered == [expected]

    def test_additional_meters_get_a_distinguishing_device_name(self) -> None:
        distinguishing = [
            template for template in self._device_names() if "meter_number" in template
        ]
        assert len(distinguishing) == 1
        assert "meter" in distinguishing[0]

    def test_all_platforms_land_on_one_device(self) -> None:
        """`DeviceInfo` is built once, in the resolver, and shared."""
        source = ENTITY_PATH.read_text(encoding="utf-8")
        assert source.count("DeviceInfo(") == 1

        for platform in ("sensor.py", "binary_sensor.py"):
            path = COMPONENT_DIR / platform
            if not path.is_file():
                continue
            assert "DeviceInfo(" not in path.read_text(encoding="utf-8"), (
                f"{platform} builds its own device instead of sharing one"
            )


class TestIdentityResolution:
    """The "first meter" decision has to be stable across restarts."""

    def _source(self) -> str:
        return ast.unparse(function(ENTITY_PATH, "_uses_legacy_identity"))

    def test_it_consults_the_entity_registry(self) -> None:
        """Registered entities are the only durable record of the old scheme."""
        source = self._source()
        assert "async_entries_for_config_entry" in source
        assert "entry.entry_id" in source, (
            "a legacy entity belonging to a different entry must not make this "
            "entry claim the legacy identity"
        )

    def test_it_falls_back_to_being_the_only_entry_for_the_account(self) -> None:
        source = self._source()
        assert "async_entries" in source
        assert "CONF_ACCOUNT_NUMBER" in source

    def test_it_does_not_mistake_a_meter_scoped_id_for_a_legacy_one(self) -> None:
        """`{account}_{meter}_{key}` also starts with `{account}_`.

        Without the meter-prefix exclusion every meter-scoped entry would
        re-read itself as legacy on the next restart and change its own
        identity -- the exact failure this whole scheme exists to prevent.
        """
        source = self._source()
        assert "meter_prefix" in source
        assert "not unique_id.startswith(meter_prefix)" in source.replace("\n", " ")

    def test_setup_falls_back_to_legacy_without_a_meter_number(self) -> None:
        """Entries predating the meter number must not key on ``None``."""
        source = ast.unparse(_resolve_identity())
        assert "meter_number is None or _uses_legacy_identity" in source


class TestPlatformsUseTheResolver:
    """A platform that resolves identity itself is a platform that drifts."""

    @pytest.mark.parametrize("platform", ["sensor.py", "binary_sensor.py"])
    def test_platform_delegates_to_resolve_identity(self, platform: str) -> None:
        path = COMPONENT_DIR / platform
        if not path.is_file():
            pytest.skip(f"{platform} does not exist yet")
        source = ast.unparse(function(path, "async_setup_entry"))
        assert "resolve_identity" in source
