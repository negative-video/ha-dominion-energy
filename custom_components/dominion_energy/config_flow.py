"""Config flow for Dominion Energy integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from dompower import (
    AccountInfo,
    ApiError,
    CannotConnectError,
    DompowerClient,
    GigyaAuthenticator,
    GigyaError,
    InvalidAuthError,
    InvalidCredentialsError,
    MeterDevice,
    TFAExpiredError,
    TFATarget,
    TFAVerificationError,
    TokenExpiredError,
)
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_NUMBER,
    CONF_COOKIES,
    CONF_COST_MODE,
    CONF_FIXED_RATE,
    CONF_HVAC_ENTITIES,
    CONF_METER_NUMBER,
    CONF_OFF_PEAK_RATE,
    CONF_PASSWORD,
    CONF_PEAK_END_HOUR,
    CONF_PEAK_RATE,
    CONF_PEAK_START_HOUR,
    CONF_PERIOD_BUDGET,
    CONF_REFRESH_TOKEN,
    CONF_SERVICE_ADDRESS,
    CONF_USERNAME,
    COST_MODE_API,
    COST_MODE_FIXED,
    COST_MODE_SCHEDULE_1,
    COST_MODE_TOU,
    DEFAULT_FIXED_RATE,
    DEFAULT_OFF_PEAK_RATE,
    DEFAULT_PEAK_END_HOUR,
    DEFAULT_PEAK_RATE,
    DEFAULT_PEAK_START_HOUR,
    DOMAIN,
)
from .rates import VA_SCHEDULE_1

_LOGGER = logging.getLogger(__name__)

# User step schema - username and password
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

# TFA code step schema
STEP_TFA_CODE_SCHEMA = vol.Schema(
    {
        vol.Required("code"): str,
    }
)


def _meter_suffix(device_id: str) -> str:
    """Return a short, human readable suffix for a meter device ID."""
    return device_id[-8:] if len(device_id) > 8 else device_id


class DominionEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dominion Energy."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._username: str | None = None
        self._password: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._cookies: dict | None = None
        self._authenticator: GigyaAuthenticator | None = None
        self._tfa_targets: list[TFATarget] = []
        self._selected_tfa_target: TFATarget | None = None
        self._account_meter_options: dict[str, tuple[AccountInfo, MeterDevice]] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return DominionEnergyOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - collect username and password."""
        errors: dict[str, str] = {}
        suggested_values: dict[str, Any] | None = None

        # Prefill form with existing data when reconfiguring
        if self.source == SOURCE_RECONFIGURE:
            reconfigure_entry = self._get_reconfigure_entry()
            suggested_values = {
                CONF_USERNAME: reconfigure_entry.data.get(CONF_USERNAME, ""),
                CONF_PASSWORD: reconfigure_entry.data.get(CONF_PASSWORD, ""),
            }

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                # Create authenticator and attempt login
                session = async_get_clientsession(self.hass)
                self._authenticator = GigyaAuthenticator(session)

                # Initialize session and submit credentials
                await self._authenticator.async_init_session()
                assert self._username is not None
                assert self._password is not None
                result = await self._authenticator.async_submit_credentials(
                    self._username, self._password
                )

                if result.tfa_required:
                    # TFA is required - proceed to TFA selection
                    return await self.async_step_tfa_select()

                # No TFA required - complete authentication
                tokens = await self._authenticator._async_complete_login()
                self._access_token = tokens.access_token
                self._refresh_token = tokens.refresh_token
                self._cookies = self._authenticator.export_cookies()

                # Proceed to account discovery
                return await self.async_step_discover_accounts()

            except InvalidCredentialsError:
                errors["base"] = "invalid_credentials"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except GigyaError as err:
                _LOGGER.error("Gigya authentication error: %s", err)
                errors["base"] = "unknown"
            except Exception:
                _LOGGER.exception("Unexpected exception during login")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, suggested_values
            ),
            errors=errors,
        )

    async def async_step_tfa_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle TFA target selection step."""
        errors: dict[str, str] = {}

        if self._authenticator is None:
            return self.async_abort(reason="unknown")

        # Get TFA options if we don't have them yet
        if not self._tfa_targets:
            try:
                self._tfa_targets = await self._authenticator.async_get_tfa_options()
            except GigyaError as err:
                _LOGGER.error("Failed to get TFA options: %s", err)
                errors["base"] = "unknown"
                # Fall back to user step
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors=errors,
                )

        if not self._tfa_targets:
            _LOGGER.error("No TFA targets available")
            errors["base"] = "unknown"
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_DATA_SCHEMA,
                errors=errors,
            )

        if user_input is not None:
            # User selected a TFA target
            selected_key = user_input["tfa_target"]

            # Find the selected target
            for target in self._tfa_targets:
                if target.id == selected_key:
                    self._selected_tfa_target = target
                    break

            if self._selected_tfa_target:
                try:
                    # Send TFA code to selected target
                    await self._authenticator.async_send_tfa_code(
                        self._selected_tfa_target
                    )
                    return await self.async_step_tfa_code()
                except GigyaError as err:
                    _LOGGER.error("Failed to send TFA code: %s", err)
                    errors["base"] = "unknown"

        # Build TFA target options
        options: dict[str, str] = {}
        for target in self._tfa_targets:
            options[target.id] = target.obfuscated

        return self.async_show_form(
            step_id="tfa_select",
            data_schema=vol.Schema(
                {
                    vol.Required("tfa_target"): vol.In(options),
                }
            ),
            errors=errors,
        )

    async def async_step_tfa_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle TFA code entry step."""
        errors: dict[str, str] = {}

        if self._authenticator is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            code = user_input["code"]

            try:
                # Verify TFA code and get tokens
                tokens = await self._authenticator.async_verify_tfa_code(code)
                self._access_token = tokens.access_token
                self._refresh_token = tokens.refresh_token
                self._cookies = self._authenticator.export_cookies()

                # Proceed to account discovery
                return await self.async_step_discover_accounts()

            except TFAVerificationError:
                errors["base"] = "tfa_failed"
            except TFAExpiredError:
                # TFA session expired - restart TFA flow
                errors["base"] = "tfa_expired"
                self._tfa_targets = []  # Clear to force re-fetch
                return await self.async_step_tfa_select()
            except GigyaError as err:
                _LOGGER.error("TFA verification error: %s", err)
                errors["base"] = "unknown"

        # Show target info in description
        target_display = (
            self._selected_tfa_target.obfuscated
            if self._selected_tfa_target
            else "your device"
        )

        return self.async_show_form(
            step_id="tfa_code",
            data_schema=STEP_TFA_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"target": target_display},
        )

    async def async_step_discover_accounts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover accounts and meters from the API."""
        errors: dict[str, str] = {}

        try:
            session = async_get_clientsession(self.hass)
            client = DompowerClient(
                session,
                access_token=self._access_token,
                refresh_token=self._refresh_token,
            )

            # Fetch customer info with all accounts
            customer_info = await client.async_get_customer_info()

            # Build selection options: "account_number|meter_id" -> (AccountInfo, MeterDevice)
            self._account_meter_options = {}
            for account in customer_info.active_accounts:
                for meter in account.meters:
                    if meter.is_active and meter.has_ami:
                        key = f"{account.account_number}|{meter.device_id}"
                        self._account_meter_options[key] = (account, meter)

            if not self._account_meter_options:
                errors["base"] = "no_meters_found"
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors=errors,
                )

            # Auto-select if only one option
            if len(self._account_meter_options) == 1:
                key = next(iter(self._account_meter_options))
                return await self._create_entry_from_selection(key)

            # Multiple options - show selection UI
            return await self.async_step_select_meter()

        except (InvalidAuthError, TokenExpiredError):
            errors["base"] = "invalid_auth"
        except CannotConnectError:
            errors["base"] = "cannot_connect"
        except ApiError as err:
            _LOGGER.error("API error discovering accounts: %s", err)
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception during account discovery")
            errors["base"] = "unknown"

        # On error, return to user step
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_select_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle meter selection when multiple accounts/meters exist."""
        if user_input is not None:
            selected_key = user_input["meter_selection"]
            return await self._create_entry_from_selection(selected_key)

        # Build display options with service address context
        options: dict[str, str] = {}
        for key, (account, meter) in self._account_meter_options.items():
            # Format: "123 Main St - Meter: ...117800"
            address = str(account.service_address)
            label = f"{address} - Meter: ...{_meter_suffix(meter.device_id)}"
            options[key] = label

        return self.async_show_form(
            step_id="select_meter",
            data_schema=vol.Schema(
                {
                    vol.Required("meter_selection"): vol.In(options),
                }
            ),
        )

    async def _create_entry_from_selection(
        self, selection_key: str
    ) -> ConfigFlowResult:
        """Create or update config entry from the selected account/meter."""
        account, meter = self._account_meter_options[selection_key]

        # Set unique ID - an entry tracks a single meter, so the account number
        # alone is not unique for customers with several meters on one account
        await self.async_set_unique_id(f"{account.account_number}_{meter.device_id}")

        new_data = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_COOKIES: self._cookies,
            CONF_ACCOUNT_NUMBER: account.account_number,
            CONF_METER_NUMBER: meter.device_id,
            CONF_SERVICE_ADDRESS: str(account.service_address),
        }

        # Handle reconfigure flow - update existing entry.
        # The unique ID is meter scoped, so selecting a different meter aborts
        # instead of repointing the entry: entity unique IDs and the external
        # statistics of the existing entry belong to the original meter, and
        # swapping the meter underneath them would mix two meters' data.
        # A different meter has to be added as its own entry.
        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data=new_data,
            )

        # New entry - check for duplicates
        self._abort_if_unique_id_configured()

        # Disambiguate the title when the account has more than one meter,
        # otherwise both entries would be named identically
        meters_on_account = sum(
            1
            for candidate_account, _ in self._account_meter_options.values()
            if candidate_account.account_number == account.account_number
        )
        title = f"Dominion Energy ({account.account_number})"
        if meters_on_account > 1:
            title = f"{title} - Meter ...{_meter_suffix(meter.device_id)}"

        return self.async_create_entry(
            title=title,
            data=new_data,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration - allows user to re-authenticate."""
        return await self.async_step_user()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth when tokens expire."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation - attempt auto-login with stored credentials."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        # Get stored credentials
        stored_username = reauth_entry.data.get(CONF_USERNAME)
        stored_password = reauth_entry.data.get(CONF_PASSWORD)

        # If we have stored credentials and no user input yet, try auto-login
        if user_input is None and stored_username and stored_password:
            try:
                session = async_get_clientsession(self.hass)
                self._authenticator = GigyaAuthenticator(session)
                self._username = stored_username
                self._password = stored_password

                # Load existing cookies to potentially bypass TFA
                existing_cookies = reauth_entry.data.get(CONF_COOKIES)
                if existing_cookies:
                    self._authenticator.import_cookies(existing_cookies)

                await self._authenticator.async_init_session()
                result = await self._authenticator.async_submit_credentials(
                    stored_username, stored_password
                )

                if result.tfa_required:
                    # TFA required - go to TFA flow
                    return await self.async_step_reauth_tfa_select()

                # No TFA - complete login and update entry
                tokens = await self._authenticator._async_complete_login()
                new_cookies = self._authenticator.export_cookies()
                new_data = {
                    **reauth_entry.data,
                    CONF_ACCESS_TOKEN: tokens.access_token,
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                    CONF_COOKIES: new_cookies,
                }
                return self.async_update_reload_and_abort(reauth_entry, data=new_data)

            except InvalidCredentialsError:
                # Stored credentials invalid - prompt for new ones
                errors["base"] = "invalid_credentials"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except GigyaError as err:
                _LOGGER.error("Reauth Gigya error: %s", err)
                errors["base"] = "unknown"
            except Exception:
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"

        # User submitted new credentials
        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                session = async_get_clientsession(self.hass)
                self._authenticator = GigyaAuthenticator(session)

                await self._authenticator.async_init_session()
                assert self._username is not None
                assert self._password is not None
                result = await self._authenticator.async_submit_credentials(
                    self._username, self._password
                )

                if result.tfa_required:
                    return await self.async_step_reauth_tfa_select()

                tokens = await self._authenticator._async_complete_login()
                new_cookies = self._authenticator.export_cookies()
                new_data = {
                    **reauth_entry.data,
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    CONF_ACCESS_TOKEN: tokens.access_token,
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                    CONF_COOKIES: new_cookies,
                }
                return self.async_update_reload_and_abort(reauth_entry, data=new_data)

            except InvalidCredentialsError:
                errors["base"] = "invalid_credentials"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except GigyaError as err:
                _LOGGER.error("Reauth Gigya error: %s", err)
                errors["base"] = "unknown"
            except Exception:
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"name": reauth_entry.title},
        )

    async def async_step_reauth_tfa_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle TFA target selection during reauth."""
        errors: dict[str, str] = {}

        if self._authenticator is None:
            return self.async_abort(reason="unknown")

        # Get TFA options if we don't have them yet
        if not self._tfa_targets:
            try:
                self._tfa_targets = await self._authenticator.async_get_tfa_options()
            except GigyaError as err:
                _LOGGER.error("Failed to get TFA options: %s", err)
                return await self.async_step_reauth_confirm()

        if not self._tfa_targets:
            _LOGGER.error("No TFA targets available")
            return await self.async_step_reauth_confirm()

        if user_input is not None:
            selected_key = user_input["tfa_target"]

            for target in self._tfa_targets:
                if target.id == selected_key:
                    self._selected_tfa_target = target
                    break

            if self._selected_tfa_target:
                try:
                    await self._authenticator.async_send_tfa_code(
                        self._selected_tfa_target
                    )
                    return await self.async_step_reauth_tfa_code()
                except GigyaError as err:
                    _LOGGER.error("Failed to send TFA code: %s", err)
                    errors["base"] = "unknown"

        options: dict[str, str] = {}
        for target in self._tfa_targets:
            options[target.id] = target.obfuscated

        return self.async_show_form(
            step_id="reauth_tfa_select",
            data_schema=vol.Schema(
                {
                    vol.Required("tfa_target"): vol.In(options),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth_tfa_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle TFA code entry during reauth."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if self._authenticator is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            code = user_input["code"]

            try:
                tokens = await self._authenticator.async_verify_tfa_code(code)
                new_cookies = self._authenticator.export_cookies()
                new_data = {
                    **reauth_entry.data,
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    CONF_ACCESS_TOKEN: tokens.access_token,
                    CONF_REFRESH_TOKEN: tokens.refresh_token,
                    CONF_COOKIES: new_cookies,
                }
                return self.async_update_reload_and_abort(reauth_entry, data=new_data)

            except TFAVerificationError:
                errors["base"] = "tfa_failed"
            except TFAExpiredError:
                errors["base"] = "tfa_expired"
                self._tfa_targets = []
                return await self.async_step_reauth_tfa_select()
            except GigyaError as err:
                _LOGGER.error("TFA verification error: %s", err)
                errors["base"] = "unknown"

        target_display = (
            self._selected_tfa_target.obfuscated
            if self._selected_tfa_target
            else "your device"
        )

        return self.async_show_form(
            step_id="reauth_tfa_code",
            data_schema=STEP_TFA_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"target": target_display},
        )


class DominionEnergyOptionsFlow(OptionsFlow):
    """Handle options flow for Dominion Energy."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._selected_mode: str | None = None

    def _save(self, updates: dict[str, Any]) -> ConfigFlowResult:
        """Persist option changes without discarding the rest.

        An options flow's `async_create_entry` *replaces* `entry.options`
        wholesale, so writing only the keys a step collected silently drops
        every option the user set in some other step. Merging keeps unrelated
        settings alive across an unrelated edit.

        Superseded keys are deliberately left in place rather than pruned:
        switching cost mode away from time-of-use and back should not lose the
        peak hours that were configured, and the readers all key off
        `cost_mode`, so a stale rate is inert. `_cost_options_signature()` only
        hashes the keys the active mode actually uses, so leftovers cannot
        trigger a spurious statistics rebuild either.
        """
        return self.async_create_entry(
            title="", data={**self._config_entry.options, **updates}
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which group of settings to edit."""
        return self.async_show_menu(step_id="init", menu_options=["cost", "insights"])

    async def async_step_insights(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the derived insight sensors."""
        if user_input is not None:
            return self._save(
                {
                    CONF_HVAC_ENTITIES: user_input[CONF_HVAC_ENTITIES],
                    CONF_PERIOD_BUDGET: user_input[CONF_PERIOD_BUDGET],
                }
            )

        options = self._config_entry.options
        current = options.get(CONF_HVAC_ENTITIES, [])

        return self.async_show_form(
            step_id="insights",
            data_schema=vol.Schema(
                {
                    # The domain is spelled out rather than imported from
                    # homeassistant.components.climate: this integration does
                    # not depend on that component and should not start
                    # loading it just to read one constant.
                    vol.Optional(CONF_HVAC_ENTITIES, default=current): (
                        selector.EntitySelector(
                            selector.EntitySelectorConfig(
                                domain="climate", multiple=True
                            )
                        )
                    ),
                    # Zero disables the budget entities rather than creating
                    # three that can never say anything.
                    vol.Optional(
                        CONF_PERIOD_BUDGET,
                        default=options.get(CONF_PERIOD_BUDGET, 0.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                }
            ),
        )

    async def async_step_cost(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select cost calculation mode."""
        if user_input is not None:
            self._selected_mode = user_input[CONF_COST_MODE]

            if self._selected_mode == COST_MODE_FIXED:
                return await self.async_step_fixed_rate()
            if self._selected_mode == COST_MODE_TOU:
                return await self.async_step_tou()
            if self._selected_mode == COST_MODE_SCHEDULE_1:
                return await self.async_step_schedule1()

            # API Estimate — no additional config needed
            return self._save({CONF_COST_MODE: COST_MODE_API})

        current_options = self._config_entry.options

        return self.async_show_form(
            step_id="cost",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COST_MODE,
                        default=current_options.get(CONF_COST_MODE, COST_MODE_API),
                    ): vol.In(
                        {
                            COST_MODE_API: "API Estimate (from bill)",
                            COST_MODE_FIXED: "Fixed Rate",
                            COST_MODE_TOU: "Time-of-Use",
                            COST_MODE_SCHEDULE_1: "Schedule 1 - VA Residential",
                        }
                    ),
                }
            ),
        )

    async def async_step_fixed_rate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2a: Configure fixed rate."""
        if user_input is not None:
            return self._save(
                {
                    CONF_COST_MODE: COST_MODE_FIXED,
                    CONF_FIXED_RATE: user_input[CONF_FIXED_RATE],
                }
            )

        current_options = self._config_entry.options

        return self.async_show_form(
            step_id="fixed_rate",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FIXED_RATE,
                        default=current_options.get(
                            CONF_FIXED_RATE, DEFAULT_FIXED_RATE
                        ),
                    ): vol.Coerce(float),
                }
            ),
        )

    async def async_step_tou(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2b: Configure time-of-use rates."""
        if user_input is not None:
            return self._save(
                {
                    CONF_COST_MODE: COST_MODE_TOU,
                    CONF_PEAK_RATE: user_input[CONF_PEAK_RATE],
                    CONF_OFF_PEAK_RATE: user_input[CONF_OFF_PEAK_RATE],
                    CONF_PEAK_START_HOUR: user_input[CONF_PEAK_START_HOUR],
                    CONF_PEAK_END_HOUR: user_input[CONF_PEAK_END_HOUR],
                }
            )

        current_options = self._config_entry.options

        return self.async_show_form(
            step_id="tou",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PEAK_RATE,
                        default=current_options.get(CONF_PEAK_RATE, DEFAULT_PEAK_RATE),
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_OFF_PEAK_RATE,
                        default=current_options.get(
                            CONF_OFF_PEAK_RATE, DEFAULT_OFF_PEAK_RATE
                        ),
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_PEAK_START_HOUR,
                        default=current_options.get(
                            CONF_PEAK_START_HOUR, DEFAULT_PEAK_START_HOUR
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                    vol.Required(
                        CONF_PEAK_END_HOUR,
                        default=current_options.get(
                            CONF_PEAK_END_HOUR, DEFAULT_PEAK_END_HOUR
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                }
            ),
        )

    async def async_step_schedule1(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2c: Confirm Schedule 1 selection."""
        if user_input is not None:
            return self._save({CONF_COST_MODE: COST_MODE_SCHEDULE_1})

        return self.async_show_form(
            step_id="schedule1",
            data_schema=vol.Schema({}),
            description_placeholders={
                "schedule_name": VA_SCHEDULE_1.name,
                "effective_date": VA_SCHEDULE_1.effective_date,
            },
        )
