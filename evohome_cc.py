"""Support for (EMEA/EU-based) Honeywell evohome systems.

Support for a temperature control system (TCS, controller) with 0+ heating
zones (e.g. TRVs, relays) and, optionally, a DHW controller.

For more details about this custom component, please refer to the docs at
https://github.com/zxdavb/evohome/
"""
# pylint: disable=deprecated-method; ZXDEL

__version__ = '0.9.6'

# Glossary:
#   TCS - temperature control system (a.k.a. Controller, Parent), which can
#   have up to 13 Children:
#     0-12 Heating zones (a.k.a. Zone), and
#     0-1 DHW controller, (a.k.a. Boiler)
# The TCS & Zones are implemented as Climate devices, Boiler as a WaterHeater

from datetime import datetime, timedelta
import logging

import requests.exceptions
import voluptuous as vol

from homeassistant.const import (
    CONF_SCAN_INTERVAL, CONF_USERNAME, CONF_PASSWORD,
    EVENT_HOMEASSISTANT_START,
    HTTP_BAD_REQUEST, HTTP_SERVICE_UNAVAILABLE, HTTP_TOO_MANY_REQUESTS,
    PRECISION_WHOLE, PRECISION_HALVES, PRECISION_TENTHS, TEMP_CELSIUS,
    STATE_OFF, STATE_ON,
)
from homeassistant.core import callback
# from homeassistant.exceptions import PlatformNotReady
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.discovery import load_platform
from homeassistant.helpers.dispatcher import (
    async_dispatcher_send,
    async_dispatcher_connect
)
from homeassistant.helpers.entity import Entity
# from homeassistant.helpers.temperature import display_temp as show_temp

# QUIREMENTS = ['https://github.com/zxdavb/evohome-client/archive/debug-version.zip#evohomeclient==0.2.8']  # noqa: E501; pylint: disable=line-too-long; ZXDEL
REQUIREMENTS = ['evohomeclient==0.2.8']

_LOGGER = logging.getLogger(__name__)

# only the controller does client API I/O during update() to get current state
# however, any entity can call methods that will change state
PARALLEL_UPDATES = 0

DOMAIN = 'evohome_cc'
DATA_EVOHOME = 'data_' + DOMAIN
DISPATCHER_EVOHOME = 'dispatcher_' + DOMAIN

DHW_TEMP = 54  # this is a guess
MIN_TEMP = 5   # minimum measured temp (not minimum setpoint)
MAX_TEMP = 35

CONF_LOCATION_IDX = 'location_idx'
SCAN_INTERVAL_DEFAULT = timedelta(seconds=300)
SCAN_INTERVAL_MINIMUM = timedelta(seconds=120)

CONF_HIGH_PRECISION = 'high_precision'
CONF_USE_HEURISTICS = 'use_heuristics'
CONF_USE_SCHEDULES = 'use_schedules'
CONF_AWAY_TEMP = 'away_temp'
CONF_OFF_TEMP = 'off_temp'
CONF_DHW_TEMP = 'dhw_target_temp'

# Validation of the user's configuration.
CV_FLOAT1 = vol.All(vol.Coerce(float), vol.Range(min=5, max=28))
CV_FLOAT2 = vol.All(vol.Coerce(float), vol.Range(min=35, max=85))

CONF_REFRESH_TOKEN = 'refresh_token'
CONF_ACCESS_TOKEN = 'access_token'
CONF_ACCESS_TOKEN_EXPIRES = 'access_token_expires'

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_LOCATION_IDX, default=0):
            cv.positive_int,
        vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL_DEFAULT):
            vol.All(cv.time_period, vol.Range(min=SCAN_INTERVAL_MINIMUM)),

        vol.Optional(CONF_HIGH_PRECISION, default=True): cv.boolean,
        vol.Optional(CONF_USE_HEURISTICS, default=False): cv.boolean,
        vol.Optional(CONF_USE_SCHEDULES, default=False): cv.boolean,

        vol.Optional(CONF_AWAY_TEMP, default=15.0): CV_FLOAT1,
        vol.Optional(CONF_OFF_TEMP, default=5.0): CV_FLOAT1,
        vol.Optional(CONF_DHW_TEMP, default=DHW_TEMP): CV_FLOAT2,

        vol.Optional(CONF_REFRESH_TOKEN, default=''): cv.string,
        vol.Optional(CONF_ACCESS_TOKEN, default=''): cv.string,
        vol.Optional(CONF_ACCESS_TOKEN_EXPIRES, default=''): cv.string,
    }),
}, extra=vol.ALLOW_EXTRA)

# These are used to help prevent E501 (line too long) violations.
GWS = 'gateways'
TCS = 'temperatureControlSystems'

# bit masks for dispatcher packets
EVO_PARENT = 0x01
EVO_CHILD = 0x02
EVO_ZONE = 0x04
EVO_DHW = 0x08
EVO_UNKNOWN = 0x10

# HA states
STATE_AUTO = 'auto'      # used in
STATE_ECO = 'eco'        # used in climate, water_heater
STATE_MANUAL = 'manual'  # used in climate

# the Controller's opmode/state and the zone's (inherited) state
EVO_RESET = 'AutoWithReset'
EVO_AUTO = 'Auto'
EVO_AUTOECO = 'AutoWithEco'
EVO_AWAY = 'Away'
EVO_DAYOFF = 'DayOff'
EVO_CUSTOM = 'Custom'
EVO_HEATOFF = 'HeatingOff'

# for the Controller. NB: evohome treats Away mode as a mode in/of itself,
# where HA considers it to 'override' the existing operating mode
TCS_STATE_TO_HA = {
    EVO_RESET: STATE_ON,
    EVO_AUTO: STATE_ON,
    EVO_AUTOECO: STATE_ECO,
    EVO_AWAY: STATE_ON,
    EVO_DAYOFF: STATE_ON,
    EVO_CUSTOM: STATE_ON,
    EVO_HEATOFF: STATE_OFF
}
TCS_STATE_TO_HA = {i: i for i, j in TCS_STATE_TO_HA.items()}                     # noqa: E501; pylint: disable=line-too-long; ZXDEL

HA_STATE_TO_TCS = {
    STATE_ON: EVO_AUTO,
    STATE_ECO: EVO_AUTOECO,
    STATE_OFF: EVO_HEATOFF
}
HA_STATE_TO_TCS = TCS_STATE_TO_HA                                                # noqa: E501; pylint: disable=line-too-long; ZXDEL

TCS_OP_LIST = list(TCS_STATE_TO_HA)

# the Zones' opmode; their state is usually 'inherited' from the TCS
EVO_FOLLOW = 'FollowSchedule'
EVO_TEMPOVER = 'TemporaryOverride'
EVO_PERMOVER = 'PermanentOverride'
EVO_OPENWINDOW = 'OpenWindow'
EVO_FROSTMODE = 'FrostProtect'

# for the Zones...
ZONE_STATE_TO_HA = {
    EVO_FOLLOW: STATE_AUTO,
    EVO_TEMPOVER: STATE_MANUAL,
    EVO_PERMOVER: STATE_MANUAL
}
ZONE_STATE_TO_HA = {i: i for i, j in ZONE_STATE_TO_HA.items()}                   # noqa: E501; pylint: disable=line-too-long; ZXDEL

HA_STATE_TO_ZONE = {
    STATE_AUTO: EVO_FOLLOW,
    STATE_MANUAL: EVO_PERMOVER
}
HA_STATE_TO_ZONE = ZONE_STATE_TO_HA                                              # noqa: E501; pylint: disable=line-too-long; ZXDEL

ZONE_OP_LIST = list(HA_STATE_TO_ZONE)

# other stuff
DHW_STATES = {STATE_ON: 'On', STATE_OFF: 'Off'}


def setup(hass, hass_config):
    """Create a (EMEA/EU-based) Honeywell evohome system.

    Currently, only the Controller and the Zones are implemented here.
    """
    # CC; pylint: disable=too-many-branches, too-many-statements
    evo_data = hass.data[DATA_EVOHOME] = {}
    evo_data['timers'] = {}

    # use a copy, since scan_interval is rounded up to nearest 60s
    evo_data['params'] = dict(hass_config[DOMAIN])
    scan_interval = evo_data['params'][CONF_SCAN_INTERVAL]
    scan_interval = timedelta(
        minutes=(scan_interval.total_seconds() + 59) // 60)

    if _LOGGER.isEnabledFor(logging.DEBUG):  # then redact username, password
        tmp = dict(evo_data['params'])
        tmp[CONF_USERNAME] = 'REDACTED'
        tmp[CONF_PASSWORD] = 'REDACTED'

        _LOGGER.debug("setup(): Configuration parameters: %s", tmp)

    from evohomeclient2 import EvohomeClient
    _LOGGER.warn("setup(): API call [4 request(s)]: client.__init__()...")       # noqa: E501; pylint: disable=line-too-long; ZXDEL

    refresh_token = evo_data['params'][CONF_REFRESH_TOKEN]
    access_token = evo_data['params'][CONF_ACCESS_TOKEN]
    access_token_expires = evo_data['params'][CONF_ACCESS_TOKEN_EXPIRES]

    if not refresh_token:
        refresh_token = None
    if not access_token or not access_token_expires:
        access_token = access_token_expires = None

    _LOGGER.warn("refresh_token: %s", refresh_token)                             # TODO: debug code, delete me
    _LOGGER.warn("access_token: %s", access_token)                               # TODO: debug code, delete me
    _LOGGER.warn("access_token_expires: %s", access_token_expires)               # TODO: debug code, delete me

    try:
        client = evo_data['client'] = EvohomeClient(
            evo_data['params'][CONF_USERNAME],
            evo_data['params'][CONF_PASSWORD],
            refresh_token=refresh_token,
            access_token=access_token,
            access_token_expires=access_token_expires,
            # debug=True
        )

    except requests.exceptions.ConnectionError as err:
        _LOGGER.error(
            "setup(): Failed to connect with the vendor's web servers. "
            "This is a networking error, possibly at the vendor's end. "
            "Unable to continue. Resolve any errors and restart HA."
        )
        _LOGGER.error("setup(): The error message is: %s", err)
        _LOGGER.error(
            "setup(): For more help, see: https://github.com/zxdavb/evohome"
        )
        return False  # unable to continue

    except requests.exceptions.HTTPError as err:
        if err.response.status_code == HTTP_BAD_REQUEST:
            _LOGGER.error(
                "setup(): Failed to connect with the vendor's web servers. "
                "Check your username (%s), and password are correct. "
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
                "You have exceeded the API rate limit. Unable to continue. "
                "Try waiting a while (say 10 minutes) and restart HA."
            )

        else:
            raise  # we don't expect/handle any other HTTPErrors

        _LOGGER.error("setup(): The error message is: %s", err)
        _LOGGER.error(
            "setup(): For more help, see: https://github.com/zxdavb/evohome"
        )
        return False  # unable to continue

    finally:  # Redact username, password as no longer needed
        evo_data['params'][CONF_USERNAME] = 'REDACTED'
        evo_data['params'][CONF_PASSWORD] = 'REDACTED'

    _LOGGER.warn("refresh_token: %s", client.refresh_token)                      # TODO: debug code, delete me
    _LOGGER.warn("access_token: %s", client.access_token)                        # TODO: debug code, delete me
    _LOGGER.warn("access_token_expires: %s", client.access_token_expires)        # TODO: debug code, delete me

    evo_data['schedules'] = {}
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
        _LOGGER.error(
            "setup(): Parameter '%s' = %s , is outside its permissible range "
            "(0-%s). Unable to continue. "
            "Check your configuration, resolve any errors and restart HA.",
            CONF_LOCATION_IDX,
            loc_idx,
            len(client.installation_info) - 1
        )

        _LOGGER.error(
            "setup(): For more help, see: https://github.com/zxdavb/evohome"
        )
        return False  # unable to continue

    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "setup(): The location (temperature control system) "
            "used is: %s [%s] (%s [%s])",
            evo_data['config']['locationInfo']['locationId'],
            evo_data['config']['locationInfo']['name'],
            evo_data['config'][GWS][0][TCS][0]['systemId'],
            evo_data['config'][GWS][0][TCS][0]['modelType']
        )
        # Some of this data needs further redaction before being logged
        tmp_loc = dict(evo_data['config'])
        tmp_loc['locationInfo']['postcode'] = 'REDACTED'

        _LOGGER.debug("setup(): evo_data['config']=%s", tmp_loc)

    if evo_data['params'][CONF_USE_HEURISTICS]:
        _LOGGER.warning(
            "setup(): '%s' = True. This feature is best efforts, and may "
            "return incorrect state data.",
            CONF_USE_HEURISTICS
        )

    load_platform(hass, 'climate', DOMAIN, {}, hass_config)

    if 'dhw' in evo_data['config'][GWS][0][TCS][0]:  # if this location has DHW
        load_platform(hass, 'water_heater', DOMAIN, {}, hass_config)

    @callback
    def _first_update(event):                                                    # noqa: E501; pylint: disable=line-too-long, unused-argument
        # When HA has started, the hub knows to retrieve it's first update
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
        """Process a dispatcher connect."""
#       _LOGGER.debug("_connect(%s): got packet %s", self._id, packet)

        if packet['to'] & self._type and packet['signal'] == 'refresh':
            # for all entity types this must have force_refresh=True
            self.async_schedule_update_ha_state(force_refresh=True)

    def _handle_exception(self, err, err_hint=None):
        """Return True if the Exception can be handled/ignored."""
        try:
            raise err

# 2/3: evohomeclient2 now (>=0.2.7) exposes requests exceptions, e.g.:
# - 'Connection aborted.', ConnectionResetError('Connection reset by peer')
# - "Max retries exceeded with url", caused by "Connection timed out"
# - NB: takes 5 mins to timeout
        except requests.exceptions.ConnectionError:
            # this appears to be common with Honeywell servers
            _LOGGER.warning(
                "The vendor's web servers appear to be uncontactable, so "
                "unable to get the latest state data during this cycle. "
                "NB: This is often a problem with the vendor's network."
            )
            return True

# 3/3: evohomeclient2 (>=0.2.7) now exposes requests exceptions, e.g.:
# - "400 Client Error: Bad Request for url" (e.g. Bad credentials)
# - "429 Client Error: Too Many Requests for url" (api usage limit exceeded)
# - "503 Client Error: Service Unavailable for url" (e.g. website down)
        except requests.exceptions.HTTPError:
            if err.response.status_code == HTTP_TOO_MANY_REQUESTS:
                _LOGGER.warning(
                    "The vendor's API rate limit has been exceeded, so "
                    "unable to get the latest state data during this cycle. "
                    "Suspending polling, and will resume after %s seconds.",
                    (self._params[CONF_SCAN_INTERVAL] * 3).total_seconds()
                )
                self._timers['statusUpdated'] = datetime.now() + \
                    self._params[CONF_SCAN_INTERVAL] * 3
                return True

            if err.response.status_code == HTTP_SERVICE_UNAVAILABLE:
                # this appears to be common with Honeywell servers
                _LOGGER.warning(
                    "The vendor's web servers appear unavailable, so "
                    "unable to get the latest state data during this cycle. "
                    "NB: This is often a problem with the vendor's network."
                )
                return True

# 1/3: evohomeclient1 (<=0.2.7) does not have a requests exceptions handler:
#     File ".../evohomeclient/__init__.py", line 33, in _populate_full_data
#       userId = self.user_data['userInfo']['userID']
#   TypeError: list indices must be integers or slices, not str

# but we can (sometimes) extract the response, which may be like this:
# [{
#   'code':    'TooManyRequests',
#   'message': 'Request count limitation exceeded, please try again later.'
# }]

        except TypeError:
            if isinstance(err_hint, list) and 'code' in err_hint[0]:
                if err_hint[0]['code'] == "TooManyRequests":
                    _LOGGER.warning(
                        "The vendor's v1 API rate limit has been exceeded, so "
                        "unable to get higher-precision (v1) temperatures. "
                        "Continuing with standard (v2) temperatures for now."
                    )
                    return True

        return False

    @property
    def name(self) -> str:
        """Return the name to use in the frontend UI."""
        _LOGGER.debug("name(%s) = %s", self._id, self._name)                      # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return self._name

    @property
    def icon(self):
        """Return the icon to use in the frontend UI."""
#       _LOGGER.debug("icon(%s) = %s", self._id, self._icon)                     # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return self._icon

    @property
    def should_poll(self) -> bool:
        """Return True if this device should be polled.

        The evohome Controller will inform its children when to update(),
        evohome child devices should never be polled.
        """
#       _LOGGER.debug("should_poll(%s) = %s", self._id, self._type == EVO_PARENT)  # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return self._type == EVO_PARENT

    @property
    def available(self) -> bool:
        """Return True if the device is currently available.

        All evohome entities are initially unavailable. Once HA has started,
        state data is then retrieved by the Controller, and then the children
        will get a state (e.g. operating_mode, current_temperature).

        However, evohome entities can become unavailable for other reasons.
        """
        no_recent_updates = self._timers['statusUpdated'] < datetime.now() - \
            self._params[CONF_SCAN_INTERVAL] * 3.1

        if no_recent_updates:
            # unavailable because no successful update()s (but why?)
            self._available = False
            debug_code = '0x01'

        elif not self._status:  # self._status == {}
            # unavailable because no status (but how? other than at startup?)
            self._available = False
            debug_code = '0x02'

        elif self._status and (self._type & EVO_CHILD):
            # (un)available because (web site via) client api says so
            self._available = \
                bool(self._status['temperatureStatus']['isAvailable'])
            debug_code = '0x03'  # only used if above is False

        else:  # is available
            self._available = True

        if not self._available and \
                self._timers['statusUpdated'] != datetime.min:
            # this isn't the first (un)available (i.e. after STARTUP)
            _LOGGER.warning(
                "available(%s) = %s (debug code %s), "
                "self._status = %s, self._timers = %s",
                self._id,
                self._available,
                debug_code,
                self._status,
                self._timers
            )

#       _LOGGER.debug("available(%s) = %s", self._id, self._available)            # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return self._available

    @property
    def supported_features(self):
        """Get the list of supported features of the Controller."""
# It will likely be the case we need to support Away/Eco/Off modes in the HA
# fashion, even though evohome's implementation of these modes are subtly
# different - this will allow tight integration with the HA landscape e.g.
# Alexa/Google integration
#       feats = self._supported_features
#       _LOGGER.debug("supported_features(%s) = %s", self._id, feats)            # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return self._supported_features

    @property
    def operation_list(self):
        """Return the list of available operations.

        Note that, for evohome, the operating mode is determined by - but not
        equivalent to - the last operation (from the operation list).
        """
        _LOGGER.debug("operation_list(%s) = %s", self._id, self._operation_list)  # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return self._operation_list

    @property
    def temperature_unit(self):
        """Return the temperature unit to use in the frontend UI."""
#       _LOGGER.debug("temperature_unit(%s) = %s", self._id, TEMP_CELSIUS)       # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return TEMP_CELSIUS

    @property
    def precision(self):
        """Return the temperature precision to use in the frontend UI."""
        if self._params[CONF_HIGH_PRECISION]:
            precision = PRECISION_TENTHS  # and is actually 0.01!
        elif self._type & EVO_PARENT:
            precision = PRECISION_HALVES
        elif self._type & EVO_ZONE:
            precision = PRECISION_HALVES
        elif self._type & EVO_DHW:
            precision = PRECISION_WHOLE

#       _LOGGER.debug("precision(%s) = %s", self._id, precision)                 # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return precision

    @property
    def current_operation(self):
        """Return the current operating mode of the evohome device."""
        if self._type & EVO_PARENT:
            evo_data = self.hass.data[DATA_EVOHOME]
            current_operation = evo_data['status']['systemModeStatus']['mode']

        elif self._type & EVO_ZONE:
            current_operation = self._status['setpointStatus']['setpointMode']

        else:  # self._type & EVO_DHW
            current_operation = self._status['stateStatus']['mode']

        _LOGGER.debug("current_operation(%s) = %s", self._id, current_operation)  # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return current_operation

    @property
    def min_temp(self):
        """Return the minimum target temp (setpoint) of a zone.

        Setpoints are 5-35C by default, but can be further limited. Only
        applies to heating zones, not DHW controllers (boilers).
        """
        if self._type & EVO_PARENT:
            temp = MIN_TEMP
        elif self._type & EVO_ZONE:
            temp = self._config['setpointCapabilities']['minHeatSetpoint']
        elif self._type & EVO_DHW:
            temp = 35
#       _LOGGER.debug("min_temp(%s) = %s", self._id, temp)                       # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return temp

    @property
    def max_temp(self):
        """Return the maximum target temp (setpoint) of a zone.

        Setpoints are 5-28C by default, but can be further limited. Only
        applies to heating zones, not DHW controllers (boilers).
        """
        if self._type & EVO_PARENT:
            temp = MAX_TEMP
        elif self._type & EVO_ZONE:
            temp = self._config['setpointCapabilities']['maxHeatSetpoint']
        elif self._type & EVO_DHW:
            temp = 85
#       _LOGGER.debug("max_temp(%s) = %s", self._id, temp)                       # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return temp


class EvoChildDevice(EvoDevice):
    """Base for Honeywell evohome child devices (Heating/DHW zones)."""

    # pylint: disable=no-member

    def __init__(self, evo_data, client, obj_ref):
        """Initialize the evohome evohome Heating/DHW zone."""
        super().__init__(evo_data, client, obj_ref)

        self._id = obj_ref.zoneId  # is also: obj_ref.dhwId

        if self._obj.zone_type == 'temperatureZone':
            self._name = obj_ref.name
            self._type = EVO_CHILD | EVO_ZONE
            self._icon = "mdi:radiator"

        elif self._obj.zone_type == 'domesticHotWater':
            self._name = "~DHW"
            self._type = EVO_CHILD | EVO_DHW
            self._icon = "mdi:thermometer-lines"

        else:  # this should never happen!
            self._type = EVO_UNKNOWN

        self._status = {}

        # children update their schedules themselves, unlike everything else
        self._schedule = evo_data['schedules'][self._id] = {}
        self._schedule['updated'] = datetime.min

    def _switchpoint(self, day_time=None, next_switchpoint=False):
        # return the switchpoint for a schedule at a particular day/time, for:
        # - heating zones: a time-from, and a target temp
        # - boilers: a time-from, and on (trying to reach target temp)/off
        schedule = self.schedule

        if day_time is None:
            day_time = datetime.now()
        day_of_week = int(day_time.strftime('%w'))  # 0 is Sunday
        time_of_day = day_time.strftime('%H:%M:%S')

        # start with the last switchpoint of the day before...
        idx = -1  # last switchpoint of the day before

        # iterate the day's switchpoints until we go past time_of_day...
        day = schedule['DailySchedules'][day_of_week]
        for i, tmp in enumerate(day['Switchpoints']):
            if time_of_day > tmp['TimeOfDay']:
                idx = i
            else:
                break

        # if asked, go for the next switchpoint...
        if next_switchpoint is True:  # the upcoming switchpoint
            if idx < len(day['Switchpoints']) - 1:
                day = schedule['DailySchedules'][day_of_week]
                switchpoint = day['Switchpoints'][idx + 1]
                switchpoint_date = day_time
            else:
                day = schedule['DailySchedules'][(day_of_week + 1) % 7]
                switchpoint = day['Switchpoints'][0]
                switchpoint_date = day_time + timedelta(days=1)

        else:  # the effective switchpoint
            if idx == -1:
                day = schedule['DailySchedules'][(day_of_week + 6) % 7]
                switchpoint = day['Switchpoints'][idx]
                switchpoint_date = day_time + timedelta(days=-1)
            else:
                day = schedule['DailySchedules'][day_of_week]
                switchpoint = day['Switchpoints'][idx]
                switchpoint_date = day_time

# TBD: remove this
        # insert day_and_time of the switchpoint for those who want it
        switchpoint['DateAndTime'] = switchpoint_date.strftime('%Y/%m/%d') + \
            " " + switchpoint['TimeOfDay']

        _LOGGER.warn(
            "_switchpoint(%s) = %s",
            self._id + " [" + self._name + "]",
            switchpoint
        )
        return switchpoint

    def _next_switchpoint_time(self) -> datetime:
        # until either the next scheduled setpoint, or just an hour from now
        if self._params[CONF_USE_SCHEDULES]:
            # get the time of the next scheduled setpoint (switchpoint)
            switchpoint = self._switchpoint(next_switchpoint=True)
            # convert back to a datetime object
            until = datetime.strptime(
                switchpoint['DateAndTime'], '%Y/%m/%d %H:%M:%S'
            )  # 'DateAndTime': '2019/01/30 19:10:00'
        else:
            # there are no schedules, so use an hour from now
            until = datetime.now() + timedelta(hours=1)

        return until

    @property
    def schedule(self):
        """Return the schedule of a zone or a DHW controller."""
        if not self._params[CONF_USE_SCHEDULES]:
            _LOGGER.warning(
                "schedule(%s): '%s' = False, so schedules are not retrieved "
                "during update(). If schedules are required, set this "
                "configuration parameter to True and restart HA.",
                self._id + " [" + self._name + "]",
                CONF_USE_SCHEDULES
            )
            return None

        _LOGGER.warn("schedule(%s) = %s", self._id, self._schedule['schedule'])
        return self._schedule['schedule']

    @property
    def device_state_attributes(self):
        """Return the optional device state attributes."""
        data = {}
        data['status'] = self._status
        data['switchpoints'] = {}

        if self._params[CONF_USE_SCHEDULES]:
            data['switchpoints']['current'] = self._switchpoint()
            data['switchpoints']['next'] = \
                self._switchpoint(next_switchpoint=True)

        _LOGGER.debug("device_state_attributes(%s) = %s", self._id, data)        # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return data

    def async_set_operation_mode(self, operation_mode):
        """Set a new target operation mode.

        This method must be run in the event loop and returns a coroutine. The
        underlying method is not asyncio-friendly.
        """
        return self.hass.async_add_job(self.set_operation_mode, operation_mode)  # noqa: E501; pylint: disable=no-member

    @property
    def current_temperature(self):
        """Return the current temperature of the Heating/DHW zone."""
        # this is used by evoZone, and evoBoiler class, however...
        # evoZone(Entity, ClimateDevice) uses temperature_unit, and
        # evoBoiler(Entity) *also* needs uses unit_of_measurement

        # TBA: this needs work - what if v1 temps failed, or ==128
        if 'apiV1Status' in self._status:
            curr_temp = self._status['apiV1Status']['temp']
        elif self._status['temperatureStatus']['isAvailable']:
            curr_temp = self._status['temperatureStatus']['temperature']
        else:
            # this isn't expected as available() should have been False
            curr_temp = None

        if curr_temp is None:
            _LOGGER.debug(
                "current_temperature(%s) - is unavailable",
                self._id
            )

        _LOGGER.debug("current_temperature(%s) = %s", self._id, curr_temp)        # noqa: E501; pylint: disable=line-too-long; ZXDEL
        return curr_temp

    def update(self):
        """Get the latest state data of the Heating/DHW zone.

        This includes state data obtained by the controller (e.g. temperature),
        but also state data obtained directly by the zone (i.e. schedule).

        This is not asyncio-friendly due to the underlying client api.
        """
# After (say) a controller.set_operation_mode, it will take a while for the
# 1. (invoked) client api call (request.xxx) to reach the web server,
# 2. web server to send message to the controller
# 3. controller to get message to zones (they'll answer immediately)
# 4. controller to send response back to web server
# 5. we make next client api call (every scan_interval)
# ... in between 1. & 5., should assumed_state/available/other be True/False?
        evo_data = self.hass.data[DATA_EVOHOME]

# Part 1: state - create pointers to state as retrieved by the controller
        if self._type & EVO_ZONE:
            for _zone in evo_data['status']['zones']:
                if _zone['zoneId'] == self._id:
                    self._status = _zone
                    break

        elif self._type & EVO_DHW:
            self._status = evo_data['status']['dhw']

        _LOGGER.debug(
            "update(%s), self._status = %s",
            self._id,
            self._status
        )

# Part 2: schedule - retrieved here as required
        if self._params[CONF_USE_SCHEDULES]:
            # lf._schedule = evo_data['schedules'][self._id]

            # Use cached schedule if < 60 mins old
            timeout = datetime.now() + timedelta(seconds=59)
            expired = timeout > self._schedule['updated'] + timedelta(hours=1)

            if expired:  # timer expired, so update schedule
                if self._type & EVO_ZONE:
                    _LOGGER.warn(
                        "update(): API call [1 request(s)]: "
                        "zone(%s).schedule()...",
                        self._id
                    )
                else:  # elif self._type & EVO_DHW:
                    _LOGGER.warn(
                        "update(): API call [1 request(s)]: "
                        "dhw(%s).schedule()...",
                        self._id
                    )
                self._schedule['schedule'] = {}
                self._schedule['updated'] = datetime.min

                try:
                    self._schedule['schedule'] = self._obj.schedule()
                except requests.exceptions.RequestException as err:
                    if not self._handle_exception(err):
                        raise
                else:
                    # only update the timers if the api call was successful
                    self._schedule['updated'] = datetime.now()

                _LOGGER.debug(
                    "update(%s), self._schedule = %s",
                    self._id,
                    self._schedule
                )

        return True
