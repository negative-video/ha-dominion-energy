"""Tests for diagnostics output, with an emphasis on redaction.

Diagnostics dumps get pasted into public GitHub issues, so the assertions here
treat any leak of a sentinel credential as a hard failure.
"""

from __future__ import annotations

from datetime import date, timedelta
import importlib.util
import json
import sys
import types
from typing import Any

import pytest

from tests.conftest import (
    COMPONENT_DIR,
    FAKE_ACCOUNT_NUMBER,
    FAKE_METER_NUMBER,
    FAKE_SECRETS,
    FakeCoordinator,
    FakeCoordinatorData,
)

# ``homeassistant`` supplies async_redact_data and is the only hard dependency
# of the module under test.
pytest.importorskip(
    "homeassistant", reason="diagnostics requires homeassistant.components.diagnostics"
)


def _load_diagnostics() -> Any:
    """Import diagnostics.py without executing the integration's __init__.

    ``custom_components.dominion_energy.__init__`` imports the coordinator,
    which pulls in ``dompower`` and the recorder. diagnostics.py needs neither,
    so it is loaded under a synthetic parent package whose ``__path__`` points
    at the component directory. That keeps this security-relevant test runnable
    (and independent of unrelated breakage in the coordinator).
    """
    pkg_name = "_dominion_diagnostics_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg

    mod_name = f"{pkg_name}.diagnostics"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(
        mod_name, COMPONENT_DIR / "diagnostics.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


diagnostics = _load_diagnostics()


def _build(
    entry_data: dict[str, Any],
    entry_options: dict[str, Any],
    coordinator: FakeCoordinator | None,
    **overrides: Any,
) -> dict[str, Any]:
    """Call build_diagnostics with entry/coordinator stand-ins."""
    kwargs: dict[str, Any] = {
        "entry_data": entry_data,
        "entry_options": entry_options,
        "entry_version": 1,
        "entry_minor_version": 1,
        "entry_source": "user",
        "entry_state": "loaded",
        "entry_disabled_by": None,
        "entry_pref_disable_polling": False,
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "last_exception": getattr(coordinator, "last_exception", None),
        "update_interval": getattr(coordinator, "update_interval", None),
        "coordinator_data": getattr(coordinator, "data", None),
        "has_statistics": True,
        "ha_version": "2026.8.1",
    }
    kwargs.update(overrides)
    return diagnostics.build_diagnostics(**kwargs)


def _walk_strings(value: Any) -> list[str]:
    """Yield every string appearing as a key or value anywhere in ``value``."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_walk_strings(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found.extend(_walk_strings(item))
    elif isinstance(value, str):
        found.append(value)
    return found


class TestRedaction:
    """The security-critical surface: nothing sensitive may escape."""

    SENSITIVE_KEYS = (
        "username",
        "password",
        "access_token",
        "refresh_token",
        "cookies",
        "account_number",
        "meter_number",
        "service_address",
    )

    def test_every_sensitive_key_is_masked(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """Each named key is present but carries the redaction marker."""
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        entry_data = result["config_entry"]["data"]

        for key in self.SENSITIVE_KEYS:
            assert key in entry_data, f"{key} unexpectedly dropped from dump"
            assert entry_data[key] == "**REDACTED**", f"{key} was not redacted"

    def test_no_sentinel_survives_anywhere_in_the_dump(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """No substring of any fake credential appears in the serialised dump.

        This is the backstop assertion: it does not care how the payload is
        structured, only that the secrets are gone.
        """
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        blob = json.dumps(result, default=str)

        for secret in FAKE_SECRETS:
            assert secret not in blob, f"{secret!r} leaked into diagnostics"

    def test_sentinels_absent_from_every_nested_string(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """Walk the structure directly, in case json.dumps hides a type."""
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        for text in _walk_strings(result):
            for secret in FAKE_SECRETS:
                assert secret not in text, f"{secret!r} leaked in {text!r}"

    def test_cookie_values_are_redacted_not_just_the_container(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """The whole cookie jar is replaced, not merely its outer keys."""
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        assert result["config_entry"]["data"]["cookies"] == "**REDACTED**"

    def test_options_are_redacted_too(self, fake_entry_data, fake_coordinator):
        """Sensitive keys are masked even when they appear under options."""
        polluted = {"cost_mode": "fixed", "password": "zzz-leaked-via-options"}
        result = _build(fake_entry_data, polluted, fake_coordinator)
        assert result["config_entry"]["options"]["password"] == "**REDACTED**"
        assert "zzz-leaked-via-options" not in json.dumps(result, default=str)

    def test_unknown_sensitive_key_added_later_is_still_caught(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """A key added to TO_REDACT is honoured across the whole payload."""
        data = {**fake_entry_data, "id_token": "zzz-leaked-id-token"}
        result = _build(data, fake_entry_options, fake_coordinator)
        assert "zzz-leaked-id-token" not in json.dumps(result, default=str)

    def test_entry_title_and_unique_id_are_never_included(
        self, fake_entry_data, fake_entry_options, fake_coordinator, fake_config_entry
    ):
        """Title and unique_id embed the account/meter number, so stay out."""
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        blob = json.dumps(result, default=str)
        assert fake_config_entry.title not in blob
        assert fake_config_entry.unique_id not in blob

    def test_exception_message_is_not_included(
        self, fake_entry_data, fake_entry_options
    ):
        """Only the exception type is reported - messages can echo tokens."""
        boom = RuntimeError(
            f"401 for account {FAKE_ACCOUNT_NUMBER} token zzz-diagnostics-access-token"
        )
        coordinator = FakeCoordinator(
            data=None, last_update_success=False, last_exception=boom
        )
        result = _build(fake_entry_data, fake_entry_options, coordinator)

        assert result["coordinator"]["last_exception_type"] == "RuntimeError"
        blob = json.dumps(result, default=str)
        assert str(boom) not in blob
        assert FAKE_ACCOUNT_NUMBER not in blob

    def test_statistic_ids_are_not_emitted(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """Statistic IDs embed the account number, so only presence is shown."""
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        blob = json.dumps(result, default=str)
        assert "_energy_consumption" not in blob
        assert result["statistics"]["has_statistics"] is True


class TestDiagnosticUsefulness:
    """Redaction must not hollow out the dump."""

    def test_reports_credential_presence_without_values(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        account = result["account"]
        assert account["has_username"] is True
        assert account["has_password"] is True
        assert account["has_access_token"] is True
        assert account["has_refresh_token"] is True
        assert account["has_cookies"] is True

    def test_missing_credentials_are_visible(
        self, fake_entry_options, fake_coordinator
    ):
        """A missing username is why auto-reauth silently gives up."""
        result = _build(
            {"account_number": FAKE_ACCOUNT_NUMBER},
            fake_entry_options,
            fake_coordinator,
        )
        assert result["account"]["has_username"] is False
        assert result["account"]["has_password"] is False

    def test_identifier_shape_is_preserved(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """Account/meter formats differ by territory and must stay visible."""
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        assert result["account"]["account_number_format"] == "12 chars, all digits"
        assert (
            result["account"]["meter_number_format"]
            == "18 chars, all digits, 9 leading zero(s)"
        )

    def test_service_region_is_exposed(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """The SC/VA distinction is the point of upstream issue #19."""
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        assert result["account"]["service_region"] == "SC"

    def test_cost_mode_and_options_are_exposed(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        assert result["cost"]["mode"] == "time_of_use"
        # Rates are user configuration, not secrets - they must survive.
        options = result["config_entry"]["options"]
        assert options["peak_rate"] == 0.15
        assert options["peak_start_hour"] == 14

    def test_cost_mode_falls_back_to_the_default(
        self, fake_entry_data, fake_coordinator
    ):
        """An unset cost_mode still reports the mode that will be applied."""
        result = _build(fake_entry_data, {}, fake_coordinator)
        assert result["cost"]["mode"] == "api_estimate"

    def test_period_dates_are_reported(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        usage = result["usage"]
        assert usage["data_date"] == "2026-08-10"
        assert usage["month_start_date"] == "2026-08-01"
        assert usage["month_end_date"] == "2026-08-10"

    def test_interval_summary(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        intervals = result["usage"]["intervals"]
        assert intervals["count"] == 48
        assert intervals["nonzero_count"] == 48
        assert intervals["days_covered"] == 1
        assert intervals["total_consumption_kwh"] == 24.0
        assert intervals["has_generation"] is False
        assert intervals["unit"] == "kWh"

    def test_generation_is_detected(
        self, fake_entry_data, fake_entry_options, fake_solar_intervals
    ):
        """Solar users need has_generation to be true."""
        data = FakeCoordinatorData(
            intervals=fake_solar_intervals, latest_interval=fake_solar_intervals[-1]
        )
        result = _build(fake_entry_data, fake_entry_options, FakeCoordinator(data=data))
        assert result["usage"]["intervals"]["has_generation"] is True

    def test_bill_forecast_summary(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        forecast = result["bill_forecast"]
        assert forecast["derived_rate"] == pytest.approx(0.125)
        assert forecast["is_tou"] is False
        assert forecast["last_bill"]["usage"] == 1000.0
        assert forecast["current_period_start"] == "2026-08-01"

    def test_coordinator_state_is_reported(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        assert result["coordinator"]["last_update_success"] is True
        assert result["coordinator"]["last_exception_type"] is None
        assert result["coordinator"]["update_interval_seconds"] == 1800.0
        assert result["coordinator"]["has_data"] is True

    def test_config_entry_metadata_is_reported(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        entry = result["config_entry"]
        assert entry["version"] == 1
        assert entry["source"] == "user"
        assert entry["state"] == "loaded"
        assert "account_number" in entry["data_keys"]

    def test_dompower_version_is_reported(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        result = _build(fake_entry_data, fake_entry_options, fake_coordinator)
        version = result["versions"]["dompower"]
        assert isinstance(version, str) and version
        assert result["versions"]["home_assistant"] == "2026.8.1"

    def test_output_is_json_serialisable(
        self, fake_entry_data, fake_entry_options, fake_coordinator
    ):
        """HA serialises diagnostics to JSON before download."""
        json.dumps(_build(fake_entry_data, fake_entry_options, fake_coordinator))


class TestDegradedStates:
    """Diagnostics must survive a broken install - that is when it is used."""

    def test_no_coordinator_data(self, fake_entry_data, fake_entry_options):
        result = _build(
            fake_entry_data,
            fake_entry_options,
            FakeCoordinator(data=None, last_update_success=False),
        )
        assert result["coordinator"]["has_data"] is False
        assert result["usage"]["intervals"]["count"] == 0
        assert result["bill_forecast"] is None

    def test_no_runtime_data_at_all(self, fake_entry_data, fake_entry_options):
        """An entry that failed setup has no coordinator attached."""
        result = _build(fake_entry_data, fake_entry_options, None)
        assert result["coordinator"]["last_update_success"] is None
        assert result["coordinator"]["has_data"] is False

    def test_empty_entry_data(self):
        result = _build({}, {}, None)
        assert result["account"]["account_number_format"] is None
        assert result["account"]["has_username"] is False
        assert result["config_entry"]["data_keys"] == []

    def test_bill_forecast_property_raising_does_not_propagate(
        self, fake_entry_data, fake_entry_options
    ):
        """A broken derived_rate must not break a support request."""

        class ExplodingForecast:
            current_period_start = date(2026, 8, 1)
            current_period_end = date(2026, 8, 31)
            current_usage_kwh = 1.0
            is_tou = False
            last_bill = None

            @property
            def derived_rate(self) -> float:
                raise ZeroDivisionError("boom")

        data = FakeCoordinatorData(bill_forecast=ExplodingForecast())  # type: ignore[arg-type]
        result = _build(fake_entry_data, fake_entry_options, FakeCoordinator(data=data))
        assert result["bill_forecast"]["derived_rate"] is None


class TestHelpers:
    """Unit coverage for the pure helpers."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("", "empty"),
            ("123456789123", "12 chars, all digits"),
            ("00123", "5 chars, all digits, 2 leading zero(s)"),
            ("ABCDEF", "6 chars, all letters"),
            ("AB12", "4 chars, alphanumeric"),
            ("AB-12", "5 chars, mixed"),
            (12345, "non-string (int)"),
        ],
    )
    def test_describe_identifier(self, value, expected):
        assert diagnostics.describe_identifier(value) == expected

    def test_describe_identifier_never_echoes_the_value(self):
        assert FAKE_METER_NUMBER not in diagnostics.describe_identifier(
            FAKE_METER_NUMBER
        )

    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            (None, None),
            ("", None),
            ("742 Evergreen Terrace, Columbia, SC 29201", "SC"),
            ("1 Main St, Richmond, VA 23220", "VA"),
            ("ServiceAddress(city='Akron', state='OH', zip_code='44301')", "OH"),
            ("somewhere unrecognisable", "unknown"),
            ("border rd, VA / NC line", "ambiguous"),
        ],
    )
    def test_describe_service_region(self, address, expected):
        assert diagnostics.describe_service_region(address) == expected

    def test_describe_service_region_returns_only_a_code(self):
        region = diagnostics.describe_service_region(
            "742 Evergreen Terrace, Columbia, SC 29201"
        )
        assert region == "SC"
        assert "Evergreen" not in region
        assert "29201" not in region

    def test_summarize_intervals_empty(self):
        summary = diagnostics.summarize_intervals([])
        assert summary["count"] == 0
        assert summary["has_generation"] is False
        assert summary["total_consumption_kwh"] is None

    def test_summarize_intervals_omits_raw_series(self, fake_intervals):
        """The half-hourly series is a behavioural fingerprint - aggregate it."""
        summary = diagnostics.summarize_intervals(fake_intervals)
        assert "intervals" not in summary
        assert "consumption" not in summary
        assert "values" not in summary
        assert summary["count"] == 48

    def test_summarize_bill_forecast_none(self):
        assert diagnostics.summarize_bill_forecast(None) is None


class TestRedactionConfig:
    """Guard the redaction list itself against regressions."""

    def test_required_keys_are_listed(self):
        required = {
            "username",
            "password",
            "access_token",
            "refresh_token",
            "cookies",
            "account_number",
            "meter_number",
            "service_address",
        }
        assert required <= set(diagnostics.TO_REDACT)

    def test_useful_keys_are_not_redacted(self):
        """Over-redaction is also a bug."""
        must_stay = {
            "cost_mode",
            "fixed_rate",
            "peak_rate",
            "off_peak_rate",
            "peak_start_hour",
            "peak_end_hour",
            "data_date",
            "last_update_success",
        }
        assert must_stay.isdisjoint(diagnostics.TO_REDACT)


@pytest.mark.asyncio
async def test_async_get_config_entry_diagnostics_end_to_end(
    fake_entry_data, fake_entry_options, fake_coordinator
):
    """Exercise the real HA entry point against stand-in objects.

    ``async_get_config_entry_diagnostics`` reads the entry defensively, so a
    stand-in entry is enough to prove the wiring without booting Home
    Assistant. The recorder lookup degrades to None, which is the documented
    behaviour when statistics cannot be queried.
    """

    class StubConfig:
        version = "2026.8.1"

    class StubHass:
        config = StubConfig()

        @staticmethod
        async def async_add_executor_job(func, *args):
            """Run inline. The real diagnostics path pushes the dompower
            version lookup to an executor because importlib.metadata touches
            the filesystem, which Home Assistant forbids in the event loop."""
            return func(*args)

    class StubEntry:
        data = fake_entry_data
        options = fake_entry_options
        version = 1
        minor_version = 1
        source = "user"
        state = "loaded"
        disabled_by = None
        pref_disable_polling = False
        runtime_data = fake_coordinator

    result = await diagnostics.async_get_config_entry_diagnostics(
        StubHass(), StubEntry()
    )

    assert result["config_entry"]["data"]["password"] == "**REDACTED**"
    assert result["versions"]["home_assistant"] == "2026.8.1"
    # Recorder is unavailable here, so statistics presence is unknown.
    assert result["statistics"]["has_statistics"] is None

    blob = json.dumps(result, default=str)
    for secret in FAKE_SECRETS:
        assert secret not in blob


def test_update_interval_without_total_seconds(
    fake_entry_data, fake_entry_options, fake_coordinator
):
    """A non-timedelta update_interval must not raise."""
    result = _build(
        fake_entry_data,
        fake_entry_options,
        fake_coordinator,
        update_interval=None,
    )
    assert result["coordinator"]["update_interval_seconds"] is None

    result = _build(
        fake_entry_data,
        fake_entry_options,
        fake_coordinator,
        update_interval=timedelta(minutes=15),
    )
    assert result["coordinator"]["update_interval_seconds"] == 900.0


class TestDompowerVersionIsInjectable:
    """The dompower version must be resolvable off the event loop.

    Regression for a live Home Assistant warning: importlib.metadata.version()
    does filesystem I/O (listdir, open, read_text), and calling it inside
    build_diagnostics ran that in the event loop every time a support dump was
    downloaded. The async caller now resolves it in an executor and passes it
    in; the direct lookup stays as a standalone fallback.
    """

    def test_injected_version_is_used_verbatim(self) -> None:
        payload = _build({}, {}, None, dompower_version="9.9.9-injected")
        assert payload["versions"]["dompower"] == "9.9.9-injected"

    def test_omitted_version_still_resolves(self) -> None:
        payload = _build({}, {}, None)
        assert isinstance(payload["versions"]["dompower"], str)
        assert payload["versions"]["dompower"]
