"""Support for Climate devices of (EMEA/EU) Honeywell evohome systems.

Specifically supports a temperature control system (TCS, controller) with 0-12
heating zones (e.g. TRVs, relays).

For more details about this platform, please refer to the documentation at
https://github.com/zxdavb/evohome/
"""
# pylint: disable=deprecated-method, unused-import; ZXDEL

__version__ = '0.9.8'

from datetime import datetime, timedelta
import logging

import requests.exceptions

from homeassistant.components.climate import (
    SUPPORT_AWAY_MODE, SUPPORT_OPERATION_MODE, SUPPORT_TARGET_TEMPERATURE,
    SUPPORT_ON_OFF,
    ClimateDevice
)
from homeassistant.const import (
    CONF_SCAN_INTERVAL,
    # STATE_OFF, STATE_ON,
    ATTR_TEMPERATURE,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from . import (
    # STATE_AUTO, STATE_ECO, STATE_MANUAL,

    DATA_EVOHOME, DISPATCHER_EVOHOME,
    CONF_LOCATION_IDX, CONF_HIGH_PRECISION, CONF_USE_HEURISTICS,
    CONF_USE_SCHEDULES, CONF_AWAY_TEMP, CONF_OFF_TEMP,

    GWS, TCS, EVO_PARENT, EVO_CHILD,
    # EVO_ZONE, EVO_DHW,

    EVO_RESET, EVO_AUTO, EVO_AUTOECO, EVO_AWAY, EVO_DAYOFF, EVO_CUSTOM,
    EVO_HEATOFF, EVO_FOLLOW, EVO_TEMPOVER, EVO_PERMOVER, EVO_FROSTMODE,

    TCS_STATE_TO_HA,
    # HA_STATE_TO_TCS, TCS_OP_LIST,
    # ZONE_STATE_TO_HA, HA_STATE_TO_ZONE,
    ZONE_OP_LIST,

    EvoDevice, EvoChildDevice,
)
ATTR_UNTIL = 'until'

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(hass, hass_config, async_add_entities,            # pylint: disable=unused-argument
                               discovery_info=None):                             # pylint: disable=unused-argument
    """Create the Controller, and its Zones, if any."""
    evo_data = hass.data[DATA_EVOHOME]

    client = evo_data['client']
    loc_idx = evo_data['params'][CONF_LOCATION_IDX]

    tcs_obj_ref = client.locations[loc_idx]._gateways[0]._control_systems[0]    # noqa E501; pylint: disable=protected-access

    _LOGGER.info(
        "setup_platform(): Found Controller, id=%s (%s), name=%s "
        "(location_idx=%s)",
        tcs_obj_ref.systemId,
        tcs_obj_ref.modelType,
        tcs_obj_ref.location.name,
        loc_idx
    )

    controller = EvoController(evo_data, client, tcs_obj_ref)
    zones = []

    for zone_idx in tcs_obj_ref.zones:
        zone_obj_ref = tcs_obj_ref.zones[zone_idx]
        _LOGGER.info(
            "setup_platform(): Found Zone, id=%s (%s), name=%s",
            zone_obj_ref.zoneId,
            zone_obj_ref.zone_type,
            zone_obj_ref.name
        )
        zones.append(EvoZone(evo_data, client, zone_obj_ref))

    async_add_entities([controller] + zones, update_before_add=False)


class EvoZone(EvoChildDevice, ClimateDevice):
    """Base for a Honeywell evohome heating Zone (e.g. a TRV)."""

    # pylint: disable=abstract-method

    def __init__(self, evo_data, client, obj_ref):
        """Initialize the evohome Zone."""
        super().__init__(evo_data, client, obj_ref)

        for _zone in evo_data['config'][GWS][0][TCS][0]['zones']:
            if _zone['zoneId'] == self._id:
                self._config = _zone
                break

        self._operation_list = ZONE_OP_LIST
        self._supported_features = \
            SUPPORT_OPERATION_MODE | \
            SUPPORT_TARGET_TEMPERATURE | \
            SUPPORT_ON_OFF

        _LOGGER.debug(
            "__init__(%s), self._config = %s",
            self._id + " [" + self._name + "]",
            self._config
        )

    @property
    def state(self):
        """Return the current state of a zone - usually, its operation mode.

        The evohome (child) devices that are in 'FollowSchedule' mode inherit
        their actual operating mode from the (parent) Controller.

        A child's state is usually its operation mode, but they can enter
        OpenWindowMode autonomously, or they can be 'Off', or just set to 4/5C.
        In all three cases, the client api seems to report 4/5C.

        This is complicated further by the possibility that the minSetPoint is
        greater than 4/5C.
        """
# When Zone is 'Off' & TCS == Away: Zone = TempOver/5C
# When Zone is 'Follow' & TCS = Away: Zone = Follow/15C
        zone_op_mode = self._status['setpointStatus']['setpointMode']

        # If possible, use inheritance to override reported state
        if zone_op_mode == EVO_FOLLOW:
            evo_data = self.hass.data[DATA_EVOHOME]
            tcs_op_mode = evo_data['status']['systemModeStatus']['mode']

            if tcs_op_mode == EVO_RESET:
                state = EVO_AUTO
            elif tcs_op_mode == EVO_HEATOFF:
                state = EVO_FROSTMODE
            else:
                state = tcs_op_mode
        else:
            state = zone_op_mode  # the reported state

        # Optionally, use heuristics to override reported state (mode)
        if self._params[CONF_USE_HEURISTICS]:
            if self._status['setpointStatus']['targetHeatTemperature'] == \
                    self.min_temp:
                if zone_op_mode == EVO_TEMPOVER:
                    # TRV turned to Off, or ?OpenWindowMode?
                    state = EVO_FROSTMODE + " (Off?)"
                elif zone_op_mode == EVO_PERMOVER:
                    state = EVO_FROSTMODE

            if state != zone_op_mode and zone_op_mode != EVO_FOLLOW:
                _LOGGER.warning(
                    "state(%s) = %s, via heuristics (but via api = %s)",
                    self._id,
                    state,
                    self._status['setpointStatus']['setpointMode']
                )
            else:
                _LOGGER.debug(
                    "state(%s) = %s, via heuristics (via api = %s)",
                    self._id,
                    state,
                    self._status['setpointStatus']['setpointMode']
                )
        else:
#           _LOGGER.debug("state(%s) = %s", self._id + " [" + self._name + "]", state)
            pass

        return state

    def _set_temperature(self, temperature, until=None):
        """Set the new target temperature of a heating zone.

        Turn the temperature for:
          - an hour/until next setpoint (TemporaryOverride), or
          - indefinitely (PermanentOverride)

        The setpoint feature requires 'use_schedules' = True.

        Keyword arguments can be:
          - temperature (required)
          - until.strftime('%Y-%m-%dT%H:%M:%SZ') is:
            - +1h for TemporaryOverride if not using schedules, or
            - next setpoint for TemporaryOverride if using schedules
            - none for PermanentOverride
        """
# If is None: PermanentOverride - override target temp indefinitely
#  otherwise: TemporaryOverride - override target temp, until some time

        max_temp = self._config['setpointCapabilities']['maxHeatSetpoint']
        if temperature > max_temp:
            _LOGGER.error(
                "set_temperature(%s): Temp %s is above maximum, %s! "
                "(cancelling call).",
                self._id + " [" + self._name + "]",
                temperature,
                max_temp
            )
            return False

        min_temp = self._config['setpointCapabilities']['minHeatSetpoint']
        if temperature < min_temp:
            _LOGGER.error(
                "set_temperature(%s): Temp %s is below minimum, %s! "
                "(cancelling call).",
                self._id + " [" + self._name + "]",
                temperature,
                min_temp
            )
            return False

        _LOGGER.warn(
            "_set_temperature(): API call [1 request(s)]: "
            "zone(%s).set_temperature(%s, %s)...",
            self._id,
            temperature,
            until
        )
        try:
            self._obj.set_temperature(temperature, until)

        except requests.exceptions.RequestException as err:
            if not self._handle_exception(err):
                raise

        return None

    def set_temperature(self, **kwargs):
        """Set a target temperature (setpoint) for a zone.

        Only applies to heating zones, not DHW controllers (boilers).
        """
        _LOGGER.debug(
            "set_temperature(%s, **kwargs=%s)",
            self._id + " [" + self._name + "]",
            kwargs.items()
        )

#       for name, value in kwargs.items():
#           _LOGGER.debug('%s = %s', name, value)

        temperature = kwargs.get(ATTR_TEMPERATURE)

        if temperature is None:
            _LOGGER.error(
                "set_temperature(%s): Temperature must not be None "
                "(cancelling call).",
                self._id + " [" + self._name + "]"
            )
            return False

# if you change the temp on via evohome, it is until next switchpoint, so...
        until = kwargs.get(ATTR_UNTIL)  # will be None as HA wont pass an until

        if until is None:
            # until either the next scheduled setpoint, or just 1 hour from now
            if self._params[CONF_USE_SCHEDULES]:
                until = self._next_switchpoint_time()
            else:
                until = datetime.now() + timedelta(hours=1)

        self._set_temperature(temperature, until)

# Optionally, use heuristics to update state
        if self._params[CONF_USE_HEURISTICS]:
            _LOGGER.debug(
                "set_operation_mode(): Action completed, "
                "updating local state data using heuristics..."
            )

            self._status['setpointStatus']['setpointMode'] \
                = EVO_PERMOVER if until is None else EVO_TEMPOVER

            self._status['setpointStatus']['targetHeatTemperature'] \
                = temperature

            _LOGGER.debug(
                "set_temperature(%s): Calling tcs.schedule_update_ha_state()",
                self._id
            )
            self.async_schedule_update_ha_state(force_refresh=False)

        return True

    def set_operation_mode(self, operation_mode, **kwargs):                      # noqa: E501; pylint: disable=arguments-differ
        # t_operation_mode(hass, operation_mode, entity_id=None):
        """Set an operating mode for a Zone.

        NB: evohome Zones do not have an operating mode as understood by HA.
        Instead they usually 'inherit' an operating mode from their controller.

        More correctly, these Zones are in a follow mode, where their setpoint
        temperatures are a function of their schedule, and the Controller's
        operating_mode, e.g. Economy mode is setpoint less (say) 3 degrees.

        Thus, you cannot set a Zone to Away mode, but the location (i.e. the
        Controller) is set to Away and each Zones's setpoints are adjusted
        accordingly (in this case, to 10 degrees by default).

        However, Zones can override these setpoints, either for a specified
        period of time, 'TemporaryOverride', after which they will revert back
        to 'FollowSchedule' mode, or indefinitely, 'PermanentOverride'.

        These three modes are treated as the Zone's operating mode and, as a
        consequence of the above, this method has 2 arguments in addition to
        operation_mode: temperature, and until.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        until = kwargs.get(ATTR_UNTIL)

        _LOGGER.debug(
            "set_operation_mode(%s, OpMode=%s, Temp=%s, Until=%s)",
            self._id + " [" + self._name + "]",
            operation_mode,
            temperature,
            until
        )

# FollowSchedule - return to scheduled target temp (indefinitely)
        if operation_mode == EVO_FOLLOW:
            if temperature is not None or until is not None:
                _LOGGER.warning(
                    "set_operation_mode(%s): For '%s' mode, 'temperature "
                    "' and 'until' should both be None (will ignore them).",
                    self._id + " [" + self._name + "]",
                    operation_mode
                )

            _LOGGER.warn(
                "set_operation_mode(%s): API call [1 request(s)]: "
                "zone.cancel_temp_override()...",
                self._id
            )
            try:
                self._obj.cancel_temp_override(self._obj)

            except requests.exceptions.RequestException as err:
                if not self._handle_exception(err):
                    raise

        else:
            if temperature is None:
                _LOGGER.warning(
                    "set_operation_mode(%s): For '%s' mode, 'temperature' "
                    "should not be None (will use current target temp).",
                    self._id,
                    operation_mode
                )
                temperature = \
                    self._status['setpointStatus']['targetHeatTemperature']

# PermanentOverride - override target temp indefinitely
        if operation_mode == EVO_PERMOVER:
            if until is not None:
                _LOGGER.warning(
                    "set_operation_mode(%s): For '%s' mode, "
                    "'until' should be None (will ignore it).",
                    self._id,
                    operation_mode
                )

            self._set_temperature(temperature, until)

# TemporaryOverride - override target temp, for a hour by default
        elif operation_mode == EVO_TEMPOVER:
            if until is None:
                _LOGGER.warning(
                    "set_operation_mode(%s): For '%s' mode, 'until' should "
                    "not be None (will use until next switchpoint).",
                    self._id,
                    operation_mode
                )
# until either the next scheduled setpoint, or just an hour from now
                if self._params[CONF_USE_SCHEDULES]:
                    until = self._next_switchpoint_time()
                else:
                    until = datetime.now() + timedelta(hours=1)

            self._set_temperature(temperature, until)

# Optionally, use heuristics to update state
        if self._params[CONF_USE_HEURISTICS]:
            _LOGGER.debug(
                "set_operation_mode(): Action completed, "
                "updating local state data using heuristics..."
            )

            self._status['setpointStatus']['setpointMode'] \
                = operation_mode

            if operation_mode == EVO_FOLLOW:
                if self._params[CONF_USE_SCHEDULES]:
                    self._status['setpointStatus']['targetHeatTemperature'] \
                        = self.setpoint
            else:
                self._status['setpointStatus']['targetHeatTemperature'] = \
                    temperature

            _LOGGER.debug(
                "Calling tcs.schedule_update_ha_state()"
            )
            self.async_schedule_update_ha_state(force_refresh=False)

        return True

    @property
    def setpoint(self):
        """Return the current (scheduled) setpoint temperature of a zone.

        This is the _scheduled_ target temperature, and not the actual target
        temperature, which would be a function of operating mode (both
        controller and zone) and, for TRVs, the OpenWindowMode feature.

        Boilers do not have setpoints; they are only on or off. Their
        (scheduled) setpoint is the same as their target temperature.
        """
        # Zones have: {'DhwState': 'On',     'TimeOfDay': '17:30:00'}
        # DHW has:    {'heatSetpoint': 17.3, 'TimeOfDay': '17:30:00'}
        setpoint = self._switchpoint()['heatSetpoint']
        _LOGGER.debug("setpoint(%s) = %s", self._id, setpoint)
        return setpoint

    @property
    def target_temperature(self):
        """Return the current target temperature of a zone.

        This is the _actual_ target temperature (a function of operating mode
        (controller and zone), and a TRVs own OpenWindowMode feature), and not
        the scheduled target temperature.
        """
# If a TRV is set to 'Off' via it's own controls, it shows up in the client api
# as 'TemporaryOverride' (not 'PermanentOverride'!), setpoint = min, until next
# switchpoint. If you change the controller mode, then
        evo_data = self.hass.data[DATA_EVOHOME]

        temp = self._status['setpointStatus']['targetHeatTemperature']

        if self._params[CONF_USE_HEURISTICS] and \
                self._params[CONF_USE_SCHEDULES]:

            tcs_op_mode = evo_data['status']['systemModeStatus']['mode']
            zone_op_mode = self._status['setpointStatus']['setpointMode']

            if tcs_op_mode == EVO_CUSTOM:
                pass  # target temps unknowable, must await update()

            elif tcs_op_mode in (EVO_AUTO, EVO_RESET) and \
                    zone_op_mode == EVO_FOLLOW:
                # target temp is set according to schedule
                temp = self.setpoint

            elif tcs_op_mode == EVO_AUTOECO and \
                    zone_op_mode == EVO_FOLLOW:
                # target temp is relative to the scheduled setpoints, with
                #  - setpoint => 16.5, target temp = (setpoint - 3)
                #  - setpoint <= 16.0, target temp = (setpoint - 0)!
                temp = self.setpoint
                if temp > 16.0:
                    temp = temp - 3

            elif tcs_op_mode == EVO_DAYOFF and \
                    zone_op_mode == EVO_FOLLOW:
                # set target temp according to schedule, but for Saturday
                this_time_saturday = datetime.now() + timedelta(
                    days=6 - int(datetime.now().strftime('%w')))
                temp = self._switchpoint(day_time=this_time_saturday)
                temp = temp['heatSetpoint']

            elif tcs_op_mode == EVO_AWAY:
                # default 'Away' temp is 15C, but can be set otherwise
                # TBC: set to CONF_AWAY_TEMP even if set setpoint is lower
                temp = self._params[CONF_AWAY_TEMP]

            elif tcs_op_mode == EVO_HEATOFF:
                # default 'HeatingOff' temp is 5C, but can be set higher
                # the target temp can't be less than a zone's minimum setpoint
                temp = max(self._params[CONF_OFF_TEMP], self.min_temp)

            if self.current_operation == EVO_FOLLOW and temp != \
                    self._status['setpointStatus']['targetHeatTemperature']:
                _LOGGER.warning(
                    "'targetHeatTemperature'(%s) = %s via heuristics "
                    "(via api = %s) - "
                    "if you can determine the cause of this discrepancy, "
                    "please consider submitting an issue via github",
                    self._id + " [" + self._name + "]",
                    temp,
                    self._status['setpointStatus']['targetHeatTemperature']
                )
            else:
                _LOGGER.debug(
                    "target_temperature(%s) = %s, via heuristics "
                    "(via api = %s)",
                    self._id + " [" + self._name + "]",
                    temp,
                    self._status['setpointStatus']['targetHeatTemperature']
                )

        else:
#           _LOGGER.debug("target_temperature(%s) = %s", self._id + " [" + self._name + "]", temp)
            pass

        return temp

    @property
    def target_temperature_step(self):
        """Return the step of setpoint (target temp) of a zone.

        Only applies to heating zones, not DHW controllers (boilers).
        """
        step = self._config['setpointCapabilities']['valueResolution']
#       step = PRECISION_HALVES
#       _LOGGER.debug("target_temperature_step(%s) = %s", self._id, step)
        return step

    def turn_off(self):
        """Turn device of."""
        _LOGGER.debug("turn_off(%s)", self._id + " [" + self._name + "]")
        self._set_temperature(self.min_temp, until=None)

    def turn_on(self):
        """Turn device on."""
        _LOGGER.debug("turn_on(%s)", self._id + " [" + self._name + "]")
        self.set_operation_mode(EVO_FOLLOW)


class EvoController(EvoDevice, ClimateDevice):
    """Base for a Honeywell evohome Controller (hub) device.

    The Controller (aka TCS, temperature control system) is the parent of all
    the child (CH/DHW) devices.  It is also a Climate device.
    """

    # pylint: disable=abstract-method

    def __init__(self, evo_data, client, obj_ref):
        """Initialize the evohome Controller (hub)."""
        super().__init__(evo_data, client, obj_ref)

        self._id = obj_ref.systemId
        self._name = obj_ref.location.name
        self._icon = "mdi:thermostat"
        self._type = EVO_PARENT

        self._config = evo_data['config'][GWS][0][TCS][0]
        self._status = evo_data['status']
        self._timers['statusUpdated'] = datetime.min

        self._operation_list = list(TCS_STATE_TO_HA)
        # lf._config['allowedSystemModes']
        self._supported_features = \
            SUPPORT_OPERATION_MODE | \
            SUPPORT_AWAY_MODE

        self._should_poll = True

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "__init__(%s), self._params = %s",
                self._id + " [" + self._name + "]",
                self._params
            )
            _LOGGER.debug(
                "__init__(%s), self._timers = %s",
                self._id + " [" + self._name + "]",
                self._timers
            )
            config = dict(self._config)
            config['zones'] = '...'
            _LOGGER.debug(
                "__init__(%s), self.config = %s",
                self._id + " [" + self._name + "]",
                config
            )

    @property
    def state(self):
        """Return the controller's current state.

        The Controller's state is usually its current operation_mode. NB: After
        calling AutoWithReset, the controller will enter Auto mode.
        """
        if self._status['systemModeStatus']['mode'] == EVO_RESET:
            state = EVO_AUTO
        else:  # usually = self.current_operation
            state = self.current_operation

#       _LOGGER.debug("state(%s) = %s", self._id + " [" + self._name + "]", state)
        return state

    @property
    def is_away_mode_on(self):
        """Return true if away mode is on."""
        away_mode = self._status['systemModeStatus']['mode'] == EVO_AWAY
#       _LOGGER.debug("is_away_mode_on(%s) = %s", self._id + " [" + self._name + "]", away_mode)
        return away_mode

    def async_set_operation_mode(self, operation_mode):
        """Set new operation mode (explicitly defined as not a ClimateDevice).

        This method must be run in the event loop and returns a coroutine.
        """
        return self.hass.async_add_job(self.set_operation_mode, operation_mode)

    def set_operation_mode(self, operation_mode):
        """Set new target operation mode for the TCS.

        'AutoWithReset may not be a mode in itself: instead, it _should_(?)
        lead to 'Auto' mode after resetting all the zones to 'FollowSchedule'.

        'HeatingOff' doesn't turn off heating, instead: it simply sets
        setpoints to a minimum value (i.e. FrostProtect mode).
        """
#       evo_data = self.hass.data[DATA_EVOHOME]

# At the start, the first thing to do is stop polled updates() until after
# set_operation_mode() has been called/effected
#       evo_data['lastUpdated'] = datetime.now()
        self._should_poll = False

        _LOGGER.debug(
            "set_operation_mode(%s, operation_mode=%s), current mode = %s",
            self._id,
            operation_mode,
            self._status['systemModeStatus']['mode']
        )

# PART 1: Call the api
        if operation_mode in list(TCS_STATE_TO_HA):
            _LOGGER.warn(
                "set_operation_mode(): API call [1 request(s)]: "
                "tcs._set_status(%s)...",
                operation_mode
            )
# These 2 lines obligate only 1 location/controller, the 3rd/4th works for 1+
# self.client._get_single_heating_system()._set_status(mode)
# self.client.set_status_normal
# self.client.locations[0]._gateways[0]._control_systems[0]._set_status(mode)
# self._obj._set_status(mode)
            try:
                self._obj._set_status(operation_mode)                            # noqa: E501; pylint: disable=protected-access

            except requests.exceptions.RequestException as err:
                if not self._handle_exception(err):
                    raise

            if self._params[CONF_USE_HEURISTICS]:
                _LOGGER.debug(
                    "set_operation_mode(%s): Using heuristics to change "
                    "operating mode from '%s' to '%s'",
                    self._id,
                    self._status['systemModeStatus']['mode'],
                    operation_mode
                )
                self._status['systemModeStatus']['mode'] = operation_mode
                self.schedule_update_ha_state(force_refresh=False)
        else:
            raise NotImplementedError()


# PART 3: HEURISTICS - update the internal state of the Zones
# For (child) Zones, when the (parent) Controller enters:
# EVO_AUTOECO, it resets EVO_TEMPOVER (but not EVO_PERMOVER) to EVO_FOLLOW
# EVO_DAYOFF,  it resets EVO_TEMPOVER (but not EVO_PERMOVER) to EVO_FOLLOW


# NEW WAY - first, set the operating modes (& states)
        if self._params[CONF_USE_HEURISTICS]:
            _LOGGER.debug(
                "set_operation_mode(): Using heuristics to change "
                "child's operating modes",
            )

            for zone in self._status['zones']:
                if operation_mode == EVO_CUSTOM:
                    pass  # operating modes unknowable, must await update()
                elif operation_mode == EVO_RESET:
                    zone['setpointStatus']['setpointMode'] = EVO_FOLLOW
                else:
                    if zone['setpointStatus']['setpointMode'] != EVO_PERMOVER:
                        zone['setpointStatus']['setpointMode'] = EVO_FOLLOW

            # this section needs more testing
            if 'dhw' in self._status:
                zone = self._status['dhw']
                if operation_mode == EVO_CUSTOM:
                    pass  # op modes unknowable, must await next update()
                elif operation_mode == EVO_RESET:
                    zone['stateStatus']['mode'] = EVO_FOLLOW
                elif operation_mode == EVO_AWAY:
                    # DHW is turned off in Away mode
                    if zone['stateStatus']['mode'] != EVO_PERMOVER:
                        zone['stateStatus']['mode'] = EVO_FOLLOW
#                       zone['stateStatus']['status'] = STATE_OFF
                else:
                    pass


# Finally, inform the Zones that their state may have changed
            pkt = {
                'sender': 'controller',
                'signal': 'update',
                'to': EVO_CHILD
            }
            async_dispatcher_send(self.hass, DISPATCHER_EVOHOME, pkt)

# At the end, the last thing to do is resume updates()
        self._should_poll = True

    def async_turn_away_mode_on(self):
        """Turn away mode on (explicitly defined as not a ClimateDevice).

        This method must be run in the event loop and returns a coroutine.
        """
#       _LOGGER.debug("async_turn_away_mode_on(%s)", self._id)
        return self.hass.async_add_job(self.turn_away_mode_on)

    def turn_away_mode_on(self):
        """Turn away mode on."""
        _LOGGER.debug("turn_away_mode_on(%s)", self._id + " [" + self._name + "]")
        self.set_operation_mode(EVO_AWAY)

    def async_turn_away_mode_off(self):
        """Turn away mode off  (explicitly defined as not a ClimateDevice).

        This method must be run in the event loop and returns a coroutine.
        """
#       _LOGGER.debug("async_turn_away_mode_off(%s)", self._id)
        return self.hass.async_add_job(self.turn_away_mode_off)

    def turn_away_mode_off(self):
        """Turn away mode off."""
        _LOGGER.debug("turn_away_mode_off(%s)", self._id + " [" + self._name + "]")
        self.set_operation_mode(EVO_AUTO)

    def _update_state_data(self, evo_data):
        client = evo_data['client']
        loc_idx = self._params[CONF_LOCATION_IDX]

    # 1. Obtain latest state data (e.g. temps)...
        _LOGGER.warn(
            "_update_state_data(): API call [1 request(s)]: "
            "client.locations[loc_idx].status()..."
        )

        _LOGGER.debug(
            "_update_state_data(): client.locations[loc_idx].locationId = %s",
            client.locations[loc_idx].locationId
        )

        try:
            evo_data['status'].update(  # or: evo_data['status'] =
                client.locations[loc_idx].status()[GWS][0][TCS][0])

        except requests.exceptions.RequestException as err:
            if not self._handle_exception(err):
                raise

        else:
            # only update the timers if the api call was successful
            self._timers['statusUpdated'] = datetime.now()

        _LOGGER.debug(
            "_update_state_data(): evo_data['status'] = %s",
            evo_data['status']
        )
        _LOGGER.debug(
            "self._timers = %s, evo_data['timers'] = %s",
            self._timers,
            evo_data['timers']
        )

    # 2. AFTER obtaining state data, do we need to increase precision of temps?
        if self._params[CONF_HIGH_PRECISION] and \
                len(client.locations) > 1:
            _LOGGER.warning(
                "Unable to increase temperature precision via the v1 api; "
                "there is more than one Location/TCS. Disabling this feature."
            )
            self._params[CONF_HIGH_PRECISION] = False

        elif self._params[CONF_HIGH_PRECISION]:
            _LOGGER.debug(
                "Trying to increase temperature precision via the v1 api..."
            )
            try:
                from evohomeclient import EvohomeClient as EvohomeClientVer1
                ec1_api = EvohomeClientVer1(client.username, client.password)

                _LOGGER.debug(
                    "_update_state_data(): Calling (v1) API [2 request(s)]: "
                    "client.temperatures()..."
                )
                # this is a a generator, so use list()
                # I think: DHW first (if any), then zones ordered by name
                new_dict_list = list(ec1_api.temperatures(force_refresh=True))

#           except requests.exceptions.RequestException as err:
#               if not self._handle_exception(err):
#                   raise

            except TypeError as err:  # v1 api doesn't use raise_for_status()
                if not self._handle_exception(err, err_hint=ec1_api.user_data):
                    # Or what else could it be?
                    _LOGGER.warning(
                        "Failed to obtain higher-precision (v1) temperatures. "
                        "Continuing with standard (v2) temperatures for now."
                    )

                    _LOGGER.debug(
                        "TypeError: ec1_api.user_data = %s",
                        ec1_api.user_data
                    )

#           except:
#               raise  # we don't handle any other exceptions

            else:
                _LOGGER.debug(
                    "_update_state_data(): new_dict_list = %s",
                    new_dict_list
                )

                # start prep of the data
                for zone in new_dict_list:
                    del zone['name']
                    zone['apiV1Status'] = {}
                    # is 128 is used for 'unavailable' temps?
                    temp = zone.pop('temp')
                    if temp != 128:
                        zone['apiV1Status']['temp'] = temp
                    else:
                        zone['apiV1Status']['temp'] = None

                # first handle the DHW, if any (done this way for readability)
                if new_dict_list[0]['thermostat'] == 'DOMESTIC_HOT_WATER':
                    dhw_v1 = new_dict_list.pop(0)

                    dhw_v1['dhwId'] = str(dhw_v1.pop('id'))
                    del dhw_v1['setpoint']
                    del dhw_v1['thermostat']

                    dhw_v2 = evo_data['status']['dhw']
                    dhw_v2.update(dhw_v1)  # more like a merge

                # now, prepare the v1 zones to merge into the v2 zones
                for zone in new_dict_list:
                    zone['zoneId'] = str(zone.pop('id'))
                    zone['apiV1Status']['setpoint'] = zone.pop('setpoint')
                    del zone['thermostat']

                org_dict_list = evo_data['status']['zones']

                _LOGGER.debug(
                    "_update_state_data(): org_dict_list = %s",
                    org_dict_list
                )

                # finally, merge the v1 zones into the v2 zones
                #  - dont use sorted(), it will create a new list!
                new_dict_list.sort(key=lambda x: x['zoneId'])
                org_dict_list.sort(key=lambda x: x['zoneId'])
                # v2 and v1 lists _should_ now be zip'ble
                for i, j in zip(org_dict_list, new_dict_list):
                    i.update(j)

            finally:
                _LOGGER.debug(
                    "_update_state_data(): evo_data['status'] = %s",
                    evo_data['status']
                )

    def update(self):
        """Get the latest state data of the installation.

        This includes state data for the Controller and its child devices, such
        as the operating_mode of the Controller and the current_temperature
        of children.

        This is not asyncio-friendly due to the underlying client api.
        """
        evo_data = self.hass.data[DATA_EVOHOME]
#       self._should_poll = True

        # Wait a minimum (scan_interval/60) mins (rounded up) between updates
        timeout = datetime.now() + timedelta(seconds=55)
        expired = timeout > self._timers['statusUpdated'] + \
            self._params[CONF_SCAN_INTERVAL]

        if not expired:  # timer not expired, so exit
            return True

# it is time to update state data
# NB: unlike all other config/state data, zones maintain their own schedules
        self._update_state_data(evo_data)
        self._status = evo_data['status']

        if _LOGGER.isEnabledFor(logging.DEBUG):
            status = dict(self._status)  # create a copy since we're editing
#           if 'zones' in status:
#               status['zones'] = '...'
#           if 'dhw' in status:
#               status['dhw'] = '...'
            _LOGGER.debug(
                "update(%s), self._status = %s",
                self._id,
                status
            )

# Finally, send a message to the children to update themselves
        pkt = {
            'sender': 'controller',
            'signal': 'refresh',
            'to': EVO_CHILD
        }

        async_dispatcher_send(self.hass, DISPATCHER_EVOHOME, pkt)

        return True

    @property
    def target_temperature(self):
        """Return the average target temperature of the Heating/DHW zones."""
        temps = [zone['setpointStatus']['targetHeatTemperature']
                 for zone in self._status['zones']]
        avg_temp = round(sum(temps) / len(temps), 1) if temps else None

#       _LOGGER.debug("target_temperature(%s) = %s", self._id + " [" + self._name + "]", avg_temp)
        return avg_temp

    @property
    def current_temperature(self):
        """Return the average current temperature of the Heating/DHW zones."""
        tmp_dict = [x for x in self._status['zones']
                    if x['temperatureStatus']['isAvailable'] is True]

        temps = [zone['temperatureStatus']['temperature'] for zone in tmp_dict]
        avg_temp = round(sum(temps) / len(temps), 1) if temps else None

#       _LOGGER.debug("current_temperature(%s) = %s", self._id + " [" + self._name + "]", avg_temp)
        return avg_temp
