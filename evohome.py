"""Support for (EMEA/EU-based) Honeywell evohome systems.

Support for a temperature control system (TCS, controller) with 0+ heating
zones (e.g. TRVs, relays) and, optionally, a DHW controller.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/evohome/
"""

# Glossary:
#   TCS - temperature control system (a.k.a. Controller, Parent), which can
#   have up to 13 Children:
#     0-12 Heating zones (a.k.a. Zone), and
#     0-1 DHW controller, (a.k.a. Boiler)
# The TCS & Zones are implemented as Climate devices, Boiler as a WaterHeater

from datetime import timedelta
import logging

from requests.exceptions import HTTPError
import voluptuous as vol

from homeassistant.const import (
    CONF_SCAN_INTERVAL, CONF_USERNAME, CONF_PASSWORD,
    EVENT_HOMEASSISTANT_START,
    HTTP_BAD_REQUEST, HTTP_SERVICE_UNAVAILABLE, HTTP_TOO_MANY_REQUESTS,
    PRECISION_HALVES, TEMP_CELSIUS,
)
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.discovery import load_platform
from homeassistant.helpers.dispatcher import (
    async_dispatcher_send,
    async_dispatcher_connect
)
from homeassistant.helpers.entity import Entity


REQUIREMENTS = ['evohomeclient==0.2.8']

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'evohome'
DATA_EVOHOME = 'data_' + DOMAIN
DISPATCHER_EVOHOME = 'dispatcher_' + DOMAIN

CONF_LOCATION_IDX = 'location_idx'
CONF_DHW_TEMP = 'dhw_temp'
DHW_TEMP_DEFAULT = 54.0  # this is a guess
DHW_TEMP_MAX = 85.0
DHW_TEMP_MIN = 35.0

SCAN_INTERVAL_DEFAULT = timedelta(seconds=300)
SCAN_INTERVAL_MINIMUM = timedelta(seconds=180)

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_LOCATION_IDX, default=0):
            cv.positive_int,
        vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL_DEFAULT):
            vol.All(cv.time_period, vol.Range(min=SCAN_INTERVAL_MINIMUM)),
        vol.Optional(CONF_DHW_TEMP, default=DHW_TEMP_DEFAULT):
            vol.All(float, vol.Range(min=DHW_TEMP_MIN, max=DHW_TEMP_MAX)),
    }),
}, extra=vol.ALLOW_EXTRA)

# These are used to help prevent E501 (line too long) violations.
GWS = 'gateways'
TCS = 'temperatureControlSystems'

# bit masks for dispatcher packets
EVO_PARENT = 0x01
EVO_CHILD = 0x02


def setup(hass, hass_config):
    """Create a (EMEA/EU-based) Honeywell evohome system.

    Currently, only the Controller and the Zones are implemented here.
    """
    evo_data = hass.data[DATA_EVOHOME] = {}
    evo_data['timers'] = {}

    # use a copy, since scan_interval is rounded up to nearest 60s
    evo_data['params'] = dict(hass_config[DOMAIN])
    scan_interval = evo_data['params'][CONF_SCAN_INTERVAL]
    scan_interval = timedelta(
        minutes=(scan_interval.total_seconds() + 59) // 60)

    from evohomeclient2 import EvohomeClient

    try:
        client = EvohomeClient(
            evo_data['params'][CONF_USERNAME],
            evo_data['params'][CONF_PASSWORD],
            debug=False
        )

    except HTTPError as err:
        if err.response.status_code == HTTP_BAD_REQUEST:
            _LOGGER.error(
                "setup(): Failed to connect with the vendor's web servers. "
                "Check your username (%s), and password are correct."
                "Unable to continue. Resolve any errors and restart HA.",
                evo_data['params'][CONF_USERNAME]
            )

        elif err.response.status_code == HTTP_SERVICE_UNAVAILABLE:
            _LOGGER.error(
                "setup(): Failed to connect with the vendor's web servers. "
                "The server is not contactable. Unable to continue. "
                "Resolve any errors and restart HA."
            )

        elif err.response.status_code == HTTP_TOO_MANY_REQUESTS:
            _LOGGER.error(
                "setup(): Failed to connect with the vendor's web servers. "
                "You have exceeded the api rate limit. Unable to continue. "
                "Wait a while (say 10 minutes) and restart HA."
            )

        else:
            raise  # we dont expect/handle any other HTTPErrors

        return False  # unable to continue

    finally:  # Redact username, password as no longer needed
        evo_data['params'][CONF_USERNAME] = 'REDACTED'
        evo_data['params'][CONF_PASSWORD] = 'REDACTED'

    evo_data['client'] = client
    evo_data['status'] = {}

    # Redact any installation data we'll never need
    for loc in client.installation_info:
        loc['locationInfo']['locationId'] = 'REDACTED'
        loc['locationInfo']['locationOwner'] = 'REDACTED'
        loc['locationInfo']['streetAddress'] = 'REDACTED'
        loc['locationInfo']['city'] = 'REDACTED'
        loc[GWS][0]['gatewayInfo'] = 'REDACTED'

    # Pull down the installation configuration
    loc_idx = evo_data['params'][CONF_LOCATION_IDX]

    try:
        evo_data['config'] = client.installation_info[loc_idx]
    except IndexError:
        _LOGGER.warning(
            "setup(): Parameter '%s'=%s, is outside its range (0-%s)",
            CONF_LOCATION_IDX,
            loc_idx,
            len(client.installation_info) - 1
        )
        return False  # unable to continue

    if _LOGGER.isEnabledFor(logging.DEBUG):
        tmp_loc = dict(evo_data['config'])
        tmp_loc['locationInfo']['postcode'] = 'REDACTED'

        _LOGGER.debug("setup(): evo_data['config']=%s", tmp_loc)

    load_platform(hass, 'climate', DOMAIN, {}, hass_config)

    if 'dhw' in evo_data['config'][GWS][0][TCS][0]:  # if this location has DHW
        load_platform(hass, 'water_heater', DOMAIN, {}, hass_config)

    @callback
    def _first_update(event):
        # When HA has started, the hub knows to retreive it's first update
        pkt = {'sender': 'setup()', 'signal': 'refresh', 'to': EVO_PARENT}
        async_dispatcher_send(hass, DISPATCHER_EVOHOME, pkt)

    hass.bus.listen(EVENT_HOMEASSISTANT_START, _first_update)

    return True


class EvoDevice(Entity):
    """Base for all Honeywell evohome devices."""

    # pylint: disable=no-member

    def __init__(self, evo_data, client, obj_ref):
        """Initialize the evohome entity."""
        self._client = client
        self._obj = obj_ref

        self._params = evo_data['params']
        self._timers = evo_data['timers']
        self._status = {}

        self._available = False  # should become True after first update()

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        async_dispatcher_connect(self.hass, DISPATCHER_EVOHOME, self._connect)

    @callback
    def _connect(self, packet):
        if packet['to'] & self._type and packet['signal'] == 'refresh':
            self.async_schedule_update_ha_state(force_refresh=True)

    def _handle_requests_exceptions(self, err):
        if err.response.status_code == HTTP_TOO_MANY_REQUESTS:
            scan_interval = self._params[CONF_SCAN_INTERVAL]
            temp_interval = max(scan_interval, SCAN_INTERVAL_DEFAULT) * 3

            _LOGGER.warning(
                "API rate limit appears to have been exceeded. "
                "Suspending polling for %s seconds.",
                temp_interval
            )

            self._timers['statusUpdated'] = datetime.now() + temp_interval

        else:
            raise err  # we dont handle any other HTTPErrors

    @property
    def name(self) -> str:
        """Return the name to use in the frontend UI."""
        return self._name

    @property
    def icon(self):
        """Return the icon to use in the frontend UI."""
        return self._icon

    @property
    def device_state_attributes(self):
        """Return the device state attributes of the evohome Climate device.

        This is state data that is not available otherwise, due to the
        restrictions placed upon ClimateDevice properties, etc. by HA.
        """
        return {'status': self._status}

    @property
    def should_poll(self) -> bool:
        """Return True if this device should be polled.

        The evohome Controller will inform its children when to update(), 
        evohome child devices should never be polled..
        """
        return self._type == EVO_PARENT

    @property
    def available(self) -> bool:
        """Return True if the device is currently available."""
        return self._available

    @property
    def supported_features(self):
        """Get the list of supported features of the device."""
        return self._supported_features

    @property
    def operation_list(self):
        """Return the list of available operations."""
        return self._operation_list

    @property
    def temperature_unit(self):
        """Return the temperature unit to use in the frontend UI."""
        return TEMP_CELSIUS

    @property
    def precision(self):
        """Return the temperature precision to use in the frontend UI."""
        return PRECISION_HALVES
