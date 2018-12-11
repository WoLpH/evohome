"""Support for Climate devices of (EMEA/EU-based) Honeywell evohome systems.

Support for a temperature control system (TCS, controller) with 0+ heating
zones (e.g. TRVs, relays).

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/climate.evohome/
"""

from datetime import datetime, timedelta
import logging

from requests.exceptions import HTTPError

from homeassistant.components.climate import (
    STATE_AUTO, STATE_ECO, STATE_MANUAL, STATE_OFF,
)
from homeassistant.components.water_heater import (
    SUPPORT_AWAY_MODE, SUPPORT_TARGET_TEMPERATURE, SUPPORT_OPERATION_MODE,
    WaterHeaterDevice,
)
from custom_components.evohome import (
    DATA_EVOHOME, DISPATCHER_EVOHOME,
    CONF_LOCATION_IDX, SCAN_INTERVAL_DEFAULT,
    CONF_DHW_TEMP, DHW_TEMP_DEFAULT, DHW_TEMP_MAX, DHW_TEMP_MIN,
    EVO_PARENT, EVO_CHILD,
    GWS, TCS,
    EvoDevice
)
from homeassistant.const import (
    CONF_SCAN_INTERVAL,
    HTTP_TOO_MANY_REQUESTS,
)
#from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

# the Controller's opmode/state and the zone's (inherited) state
EVO_RESET = 'AutoWithReset'
EVO_AUTO = 'Auto'
EVO_AUTOECO = 'AutoWithEco'
EVO_AWAY = 'Away'
EVO_DAYOFF = 'DayOff'
EVO_CUSTOM = 'Custom'
EVO_HEATOFF = 'HeatingOff'

# these are for Zones' opmode, and state
EVO_FOLLOW = 'FollowSchedule'
EVO_TEMPOVER = 'TemporaryOverride'
EVO_PERMOVER = 'PermanentOverride'

# for the Controller. NB: evohome treats Away mode as a mode in/of itself,
# where HA considers it to 'override' the exising operating mode
TCS_STATE_TO_HA = {
    EVO_RESET: STATE_AUTO,
    EVO_AUTO: STATE_AUTO,
    EVO_AUTOECO: STATE_ECO,
    EVO_AWAY: STATE_AUTO,
    EVO_DAYOFF: STATE_AUTO,
    EVO_CUSTOM: STATE_AUTO,
    EVO_HEATOFF: STATE_OFF
}
HA_STATE_TO_TCS = {
    STATE_AUTO: EVO_AUTO,
    STATE_ECO: EVO_AUTOECO,
    STATE_OFF: EVO_HEATOFF
}
TCS_OP_LIST = list(HA_STATE_TO_TCS)

# the Zones' opmode; their state is usually 'inherited' from the TCS
EVO_FOLLOW = 'FollowSchedule'
EVO_TEMPOVER = 'TemporaryOverride'
EVO_PERMOVER = 'PermanentOverride'

# for the Zones...
ZONE_STATE_TO_HA = {
    EVO_FOLLOW: STATE_AUTO,
    EVO_TEMPOVER: STATE_MANUAL,
    EVO_PERMOVER: STATE_MANUAL
}
HA_STATE_TO_ZONE = {
    STATE_AUTO: EVO_FOLLOW,
    STATE_MANUAL: EVO_PERMOVER
}
ZONE_OP_LIST = list(HA_STATE_TO_ZONE)


async def async_setup_platform(hass, hass_config, async_add_entities,
                               discovery_info=None):
    """Create the evohome DHW Controller, if any."""
    evo_data = hass.data[DATA_EVOHOME]

    client = evo_data['client']
    loc_idx = evo_data['params'][CONF_LOCATION_IDX]

    # evohomeclient has exposed no means of accessing non-default location
    # (i.e. loc_idx > 0) other than using a protected member, such as below
    tcs_obj_ref = client.locations[loc_idx]._gateways[0]._control_systems[0]    # noqa E501; pylint: disable=protected-access

    _LOGGER.debug(
        "setup_platform(): Using Controller, id=%s [%s], "
        "name=%s (location_idx=%s)",
        tcs_obj_ref.systemId,
        tcs_obj_ref.modelType,
        tcs_obj_ref.location.name,
        loc_idx
    )

    if tcs_obj_ref.hotwater:
        _LOGGER.info(
            "setup(): Found DHW device, id: %s, type: %s",
            tcs_obj_ref.hotwater.zoneId,  # same has .dhwId
            tcs_obj_ref.hotwater.zone_type
        )
        dhw = EvoDHW(evo_data, client, tcs_obj_ref.hotwater)

    entities = [dhw]

    async_add_entities(entities, update_before_add=False)


class EvoDHW(EvoDevice, WaterHeaterDevice):
    """Base for a Honeywell evohome DHW device."""

    def __init__(self, evo_data, client, obj_ref):
        """Initialize the evohome Zone."""
        super().__init__(evo_data, client, obj_ref)

        self._id = obj_ref.zoneId
        self._name = "~DHW controller"
        self._icon = "mdi:thermometer-lines"
        self._type = EVO_CHILD

        self._config = evo_data['config'][GWS][0][TCS][0]['dhw']
        self._status = {}

        self._operation_list = ZONE_OP_LIST
        self._supported_features = \
            SUPPORT_OPERATION_MODE

    @property
    def state(self):
        """Return the current state."""
        _LOGGER.warn("state(%s) = %s", self._id, self._status['stateStatus']['state'])
        return self._status['stateStatus']['state']

    @property
    def current_operation(self):
        """Return the current operating mode of the evohome Zone.

        The evohome Zones that are in 'FollowSchedule' mode inherit their
        actual operating mode from the Controller.
        """
        evo_data = self.hass.data[DATA_EVOHOME]

        system_mode = evo_data['status']['systemModeStatus']['mode']
        setpoint_mode = self._status['stateStatus']['mode']

        if setpoint_mode == EVO_FOLLOW:
            # then inherit state from the controller
            if system_mode == EVO_RESET:
                current_operation = TCS_STATE_TO_HA.get(EVO_AUTO)
            else:
                current_operation = TCS_STATE_TO_HA.get(system_mode)
        else:
            current_operation = ZONE_STATE_TO_HA.get(setpoint_mode)

        _LOGGER.warn("current_operation(%s) = %s", self._id, current_operation)
        return current_operation

    @property
    def temperature(self):
        """Return the current temperature of the evohome DHW controller."""
        temp = self._status['temperatureStatus']['temperature']
        _LOGGER.warn("temperature(%s) = %s", self._id, temp)
        return temp

    @property
    def target_temperature(self):
        """Return the target temperature of the evohome DHW controller.
        
        This is not configurable/reportable, so we use a static value."""
        evo_data = self.hass.data[DATA_EVOHOME]
        temp = evo_data['params'][CONF_DHW_TEMP]
        _LOGGER.warn("target_temperature(%s) = %s", self._id, temp)
        return None  # is: Temp, or: None ?

    @property
    def is_away_mode_on(self):
        """Return true if away mode is on."""
        _LOGGER.warn("is_away_mode_on(%s) = %s", self._id, None)
        return None

    def set_temperature(self, **kwargs):
        """Set new target temperature."""
        raise NotImplementedError()

    def set_operation_mode(self, operation_mode):
        """Set new target operation mode."""
        raise NotImplementedError()

    def turn_away_mode_on(self):
        """Turn away mode on."""
        raise NotImplementedError()

    def turn_away_mode_off(self):
        """Turn away mode off."""
        raise NotImplementedError()

    @property
    def min_temp(self):
        """Return the minimum temperature."""
#       return convert_temperature(DHW_TEMP_MIN, TEMP_CELSIUS, self.temperature_unit)
        _LOGGER.warn("min_temp(%s) = %s", self._id, DHW_TEMP_MIN)
        return DHW_TEMP_MIN

    @property
    def max_temp(self):
        """Return the maximum temperature."""
#       return convert_temperature(DHW_TEMP_MAX, TEMP_CELSIUS, self.temperature_unit)
        _LOGGER.warn("max_temp(%s) = %s", self._id, DHW_TEMP_MAX)
        return DHW_TEMP_MAX

    def update(self):
        """Process the evohome Zone's state data."""
        evo_data = self.hass.data[DATA_EVOHOME]

        self._status = evo_data['status']['dhw']

        self._available = True
