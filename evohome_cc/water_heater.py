"""Support for WaterHeater devices of (EMEA/EU) Honeywell evohome systems.

Specifically supports a DHW controller (the temperature control system is
supported as a Climate device).

For more details about this platform, please refer to the documentation at
https://github.com/zxdavb/evohome/
"""
# pylint: disable=deprecated-method, unused-import; ZXDEL

__version__ = '0.9.6'

from datetime import datetime, timedelta
import logging

import requests.exceptions

# from homeassistant.components.climate import (
#    ClimateDevice
# )
from homeassistant.components.water_heater import (
    # SUPPORT_AWAY_MODE, SUPPORT_TARGET_TEMPERATURE,
    SUPPORT_OPERATION_MODE,
    WaterHeaterDevice
)
from homeassistant.const import (
    # CONF_SCAN_INTERVAL,
    STATE_OFF, STATE_ON,
    # ATTR_TEMPERATURE,
)
from . import (
    # STATE_AUTO, STATE_ECO, STATE_MANUAL,

    DATA_EVOHOME,  # DISPATCHER_EVOHOME,
    CONF_LOCATION_IDX, CONF_USE_HEURISTICS, CONF_USE_SCHEDULES,
    # CONF_AWAY_TEMP, CONF_HIGH_PRECISION, CONF_OFF_TEMP,
    CONF_DHW_TEMP, DHW_STATES,

    GWS, TCS,
    # EVO_PARENT, EVO_CHILD, EVO_ZONE, EVO_DHW,

    EVO_AWAY,
    # EVO_RESET, EVO_AUTO, EVO_AUTOECO, EVO_DAYOFF, EVO_CUSTOM, EVO_HEATOFF,
    EVO_FOLLOW, EVO_TEMPOVER,  # EVO_PERMOVER, EVO_FROSTMODE,

    # TCS_STATE_TO_HA, HA_STATE_TO_TCS, TCS_OP_LIST,
    # ZONE_STATE_TO_HA, HA_STATE_TO_ZONE,
    ZONE_OP_LIST,

    # EvoDevice,
    EvoChildDevice,
)
ATTR_UNTIL = 'until'

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(hass, hass_config, async_add_entities,
                               discovery_info=None):
    """Create the DHW controller."""
    evo_data = hass.data[DATA_EVOHOME]

    client = evo_data['client']
    loc_idx = evo_data['params'][CONF_LOCATION_IDX]

    tcs_obj_ref = client.locations[loc_idx]._gateways[0]._control_systems[0]    # noqa E501; pylint: disable=protected-access

    _LOGGER.info(
        "setup(): Found DHW device, id: %s (%s)",
        tcs_obj_ref.hotwater.zoneId,  # same has .dhwId
        tcs_obj_ref.hotwater.zone_type
    )

    dhw = EvoDHW(evo_data, client, tcs_obj_ref.hotwater)

    async_add_entities([dhw], update_before_add=False)


class EvoDHW(EvoChildDevice, WaterHeaterDevice):
    """Base for a Honeywell evohome DHW controller (aka boiler)."""

    # pylint: disable=abstract-method

    def __init__(self, evo_data, client, obj_ref):
        """Initialize the evohome DHW controller."""
        super().__init__(evo_data, client, obj_ref)

        self._config = evo_data['config'][GWS][0][TCS][0]['dhw']

        self._operation_list = ZONE_OP_LIST

        self._supported_features = \
            SUPPORT_OPERATION_MODE

        _LOGGER.debug("__init__(%s), self._config = %s", self._id + " [" + self._name + "]", self._config)

    @property
    def target_temperature(self):
        """TBD: Return None, as there is no target temp exposed via the api."""
        temp = self._params[CONF_DHW_TEMP]

        _LOGGER.debug("target_temperature(%s) = %s", self._id + " [" + self._name + "]", temp)
        return temp

    def _set_dhw_state(self, state=None, mode=None, until=None):
        """Set the new state of a DHW controller.

        Turn the DHW on/off for an hour, until next setpoint, or indefinitely.
        The setpoint feature requires 'use_schedules' = True.

        Keyword arguments can be:
          - state  = "On" | "Off" (no default)
          - mode  = "TemporaryOverride" (default) | "PermanentOverride"
          - until.strftime('%Y-%m-%dT%H:%M:%SZ') is:
            - +1h for TemporaryOverride if not using schedules
            - next setpoint for TemporaryOverride if using schedules
            - ignored for PermanentOverride
        """
        _LOGGER.warn("_set_dhw_state(%s): state=%s, mode=%s, until=%s", self._id + " [" + self._name + "]", state, mode, until)

        if state is None:
            state = self._status['stateStatus']['state']
        if mode is None:
            mode = EVO_TEMPOVER

        if mode != EVO_TEMPOVER:
            until = None
        else:
            if until is None:
                if self._params[CONF_USE_SCHEDULES]:
                    until = self._next_switchpoint_time
                else:
                    until = datetime.now() + timedelta(hours=1)

        if until is not None:
            until = until.strftime('%Y-%m-%dT%H:%M:%SZ')

        data = {'State': state, 'Mode': mode, 'UntilTime': until}

        _LOGGER.warn(
            "_set_dhw_state(%s): API call [1 request(s)]: dhw._set_dhw(%s)...",
            self._id,
            data
        )

        try:
            self._obj._set_dhw(data)                                            # noqa: E501; pylint: disable=protected-access

        except requests.exceptions.HTTPError as err:
            if not self._handle_exception(err):
                raise

        if self._params[CONF_USE_HEURISTICS]:
            self._status['stateStatus']['state'] = state
            self._status['stateStatus']['mode'] = mode
            self.async_schedule_update_ha_state(force_refresh=False)

    @property
    def state(self):
        """Return the state of a DHW controller.

        Reportable State can be:
          - On, working to raise current temp until equal to target temp
          - Off, current temp is ignored
          - Away, Off regardless of scheduled state
        """
        evo_data = self.hass.data[DATA_EVOHOME]

        tcs_op_mode = evo_data['status']['systemModeStatus']['mode']
        dhw_op_mode = self._status['stateStatus']['mode']

        # Determine the reported state
        dhw_state = self._status['stateStatus']['state']

        if dhw_state == DHW_STATES[STATE_ON]:
            state = STATE_ON
        else:  # dhw_state == DHW_STATES[STATE_OFF]:
            state = STATE_OFF

        # If possible, use inheritance to override reported state
        if dhw_op_mode == EVO_FOLLOW:
            if tcs_op_mode == EVO_AWAY:
                state = EVO_AWAY  # a special form of 'Off'

        # Perform a sanity check & warn if it fails
        if state == EVO_AWAY:
            if dhw_state != DHW_STATES[STATE_OFF]:
                _LOGGER.warning(
                    "state(%s) = %s, via inheritance (via api = %s)",
                    self._id,
                    state,
                    dhw_state
                )
            else:
                _LOGGER.debug(
                    "state(%s) = %s, via inheritance (via api = %s)",
                    self._id,
                    state,
                    dhw_state
                )
        else:
            _LOGGER.debug("state(%s) = %s", self._id, state)
        return state

    @property
    def is_on(self):
        """Return True if DHW is on (albeit regulated by thermostat)."""
        is_on = (self.state == DHW_STATES[STATE_ON])

        _LOGGER.debug("is_on(%s) = %s", self._id + " [" + self._name + "]", is_on)
        return is_on

    def turn_on(self):
        """Turn DHW on for an hour, until next setpoint, or indefinitely."""
        mode = EVO_TEMPOVER
        until = None

        _LOGGER.debug("turn_on(%s, mode=%s, until=%s)", self._id + " [" + self._name + "]", mode, until)

        self._set_dhw_state(DHW_STATES[STATE_ON], mode, until)

    def turn_off(self):
        """Turn DHW off for an hour, until next setpoint, or indefinitely."""
        mode = EVO_TEMPOVER
        until = None

        _LOGGER.debug("turn_off(%s, mode=%s, until=%s)", self._id + " [" + self._name + "]", mode, until)

        self._set_dhw_state(DHW_STATES[STATE_OFF], mode, until)

    def set_operation_mode(self, operation_mode):
        """Set new operation mode for a DHW controller."""
#       _LOGGER.debug("set_operation_mode(%s, operation_mode=%s)", self._id + " [" + self._name + "]", operation_mode )

# FollowSchedule - return to scheduled target temp (indefinitely)
        if operation_mode == EVO_FOLLOW:
            state = ''
        else:
            state = self._status['stateStatus']['state']

# PermanentOverride - override target temp indefinitely
# TemporaryOverride - override target temp, for a period of time
        if operation_mode == EVO_TEMPOVER:
            if self._params[CONF_USE_SCHEDULES]:
                until = self._next_switchpoint_time
            else:
                until = datetime.now() + timedelta(hours=1)

            until = until.strftime('%Y-%m-%dT%H:%M:%SZ')

        else:
            until = None

        self._set_dhw_state(state, operation_mode, until)
