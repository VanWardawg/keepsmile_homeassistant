import asyncio
from homeassistant.components import bluetooth
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.components.light import (ColorMode)
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTCharacteristic, BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    ble_device_has_changed,
    establish_connection,
)
from typing import Any, TypeVar, cast, Tuple
from collections.abc import Callable
import traceback
import logging
import colorsys


# Add effects information in a separate file because there is a LOT of boilerplate.
# from .effects import (
#     EFFECT_MAP,
#     EFFECT_LIST,
#     EFFECT_ID_TO_NAME
# )

LOGGER = logging.getLogger(__name__)

EFFECT_0x00 = "colorloop"
EFFECT_0x01 = "red_fade"
EFFECT_0x02 = "green_fade"
EFFECT_0x03 = "blue_fade"
EFFECT_0x04 = "yellow_fade"
EFFECT_0x05 = "cyan_fade"
EFFECT_0x06 = "purple_fade"
EFFECT_0x07 = "white_fade"
EFFECT_RED_GREEN_BLUE_CROSS_FADE = "rgb_cross_fade"
EFFECT_0x08 = "rg_cross_fade"
EFFECT_0x09 = "rb_cross_fade"
EFFECT_0x0a = "gb_cross_fade"
EFFECT_0x0c = "colorstrobe"
EFFECT_0x0d = "red_strobe"
EFFECT_0x0e = "green_strobe"
EFFECT_0x0f = "blue_strobe"
EFFECT_0x10 = "yellow_strobe"
EFFECT_0x11 = "cyan_strobe"
EFFECT_0x12 = "purple_strobe"
EFFECT_0x13 = "white_strobe"
EFFECT_CYCLE_RGB = "cycle_rgb"
EFFECT_CYCLE_SEVEN_COLORS = "cycle_seven_colors"
EFFECT_0x14 = "colorjump"
EFFECT_CUSTOM = "custom"

EFFECT_MAP = {
    EFFECT_0x00:    0x00,
    EFFECT_0x01:    0x01,
    EFFECT_0x02:    0x02,
    EFFECT_0x03:    0x03,
    EFFECT_0x04:    0x04,
    EFFECT_0x05:    0x05,
    EFFECT_0x06:    0x06,
    EFFECT_0x07:    0x07,
    EFFECT_0x08:    0x08,
    EFFECT_0x09:    0x09,
    EFFECT_0x0a:    0x0a,
    EFFECT_0x0c:    0x0c,
    EFFECT_0x0d:    0x0d,
    EFFECT_0x0e:    0x0e,
    EFFECT_0x0f:    0x0f,
    EFFECT_0x10:    0x10,
    EFFECT_0x11:    0x11,
    EFFECT_0x12:    0x12,
    EFFECT_0x13:    0x13,
    EFFECT_0x14:    0x14,
}

EFFECT_LIST = sorted(EFFECT_MAP)
EFFECT_ID_NAME = {v: k for k, v in EFFECT_MAP.items()}

NAME_ARRAY = ["BJ_LED"]
WRITE_CHARACTERISTIC_UUIDS = ["0000ee01-0000-1000-8000-00805f9b34fb"]
TURN_ON_CMD  = [bytearray.fromhex("69 96 02 01 01")]
TURN_OFF_CMD = [bytearray.fromhex("69 96 02 01 00")]
DEFAULT_ATTEMPTS = 3
BLEAK_BACKOFF_TIME = 0.25
RETRY_BACKOFF_EXCEPTIONS = (BleakDBusError)

WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])

def retry_bluetooth_connection_error(func: WrapFuncType) -> WrapFuncType:
    async def _async_wrap_retry_bluetooth_connection_error(
        self: "BJLEDInstance", *args: Any, **kwargs: Any
    ) -> Any:
        attempts = DEFAULT_ATTEMPTS
        max_attempts = attempts - 1

        for attempt in range(attempts):
            try:
                return await func(self, *args, **kwargs)
            except BleakNotFoundError:
                # The lock cannot be found so there is no
                # point in retrying.
                raise
            except RETRY_BACKOFF_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    LOGGER.debug(
                        "%s: %s error calling %s, reach max attempts (%s/%s)",
                        self.name,
                        type(err),
                        func,
                        attempt,
                        max_attempts,
                        exc_info=True,
                    )
                    raise
                LOGGER.debug(
                    "%s: %s error calling %s, backing off %ss, retrying (%s/%s)...",
                    self.name,
                    type(err),
                    func,
                    BLEAK_BACKOFF_TIME,
                    attempt,
                    max_attempts,
                    exc_info=True,
                )
                await asyncio.sleep(BLEAK_BACKOFF_TIME)
            except BLEAK_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    LOGGER.debug(
                        "%s: %s error calling %s, reach max attempts (%s/%s): %s",
                        self.name,
                        type(err),
                        func,
                        attempt,
                        max_attempts,
                        err,
                        exc_info=True,
                    )
                    raise
                LOGGER.debug(
                    "%s: %s error calling %s, retrying  (%s/%s)...: %s",
                    self.name,
                    type(err),
                    func,
                    attempt,
                    max_attempts,
                    err,
                    exc_info=True,
                )

    return cast(WrapFuncType, _async_wrap_retry_bluetooth_connection_error)


def rgb_to_hsv(r,g,b):
    h, s, v = colorsys.rgb_to_hsv(r/255.0,g/255.0,b/255.0)
    h, s, v = int(h*360), int(s*100), int(v*100)
    return [h,s,v]

class BJLEDInstance:
    def __init__(self, address, reset: bool, delay: int, hass) -> None:
        self.loop = asyncio.get_running_loop()
        self._mac = address
        self._reset = reset
        self._delay = delay
        self._hass = hass
        self._device: BLEDevice | None = None
        self._device = bluetooth.async_ble_device_from_address(self._hass, address)
        if not self._device:
            raise ConfigEntryNotReady(
                f"You need to add bluetooth integration (https://www.home-assistant.io/integrations/bluetooth) or couldn't find a nearby device with address: {address}"
            )
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._cached_services: BleakGATTServiceCollection | None = None
        self._expected_disconnect = False
        self._packet_counter = 0
        self._is_on = None
        self._rgb_color = None
        self._brightness = 255
        self._effect = None
        self._effect_speed = 0x64
        self._color_temp_kelvin = None
        self._color_mode = ColorMode.RGB
        self._write_uuid = None
        self._read_uuid = None
        self._turn_on_cmd = None
        self._turn_off_cmd = None
        self._max_color_temp_kelvin = None
        self._min_color_temp_kelvin = None
        self._model = self._detect_model()
        #self._on_update_callbacks = []
        
        LOGGER.debug(
            "Model information for device %s : ModelNo %s. MAC: %s",
            self._device.name,
            self._model,
            self._mac,
        )

    def _detect_model(self):
        x = 0
        for name in NAME_ARRAY:
            if self._device.name.lower().startswith(name.lower()):
                self._turn_on_cmd = TURN_ON_CMD[x]
                self._turn_off_cmd = TURN_OFF_CMD[x]
                #self._max_color_temp_kelvin = MAX_COLOR_TEMPS_K[x]
                #self._min_color_temp_kelvin = MIN_COLOR_TEMPS_K[x]
                return x
            x = x + 1

    async def _write(self, data: bytearray):
        """Send command to device and read response."""
        await self._ensure_connected()
        await self._write_while_connected(data)

    async def _write_while_connected(self, data: bytearray):
        LOGGER.debug(f"Writing data to {self.name}: {data}")
        await self._client.write_gatt_char(self._write_uuid, data, False)
    
    
    @property
    def mac(self):
        return self._device.address

    @property
    def reset(self):
        return self._reset

    @property
    def name(self):
        return self._device.name

    @property
    def rssi(self):
        return self._device.rssi

    @property
    def is_on(self):
        return self._is_on

    @property
    def brightness(self):
        return self._brightness 

    @property
    def rgb_color(self):
        return self._rgb_color
    
    @property
    def min_color_temp_kelvin(self):
        return self._min_color_temp_kelvin

    @property
    def max_color_temp_kelvin(self):
        return self._max_color_temp_kelvin

    @property
    def color_temp_kelvin(self):
        return self._color_temp_kelvin

    @property
    def hs_color(self):
        return self._hs_color

    @property
    def effect_list(self) -> list[str]:
        return EFFECT_LIST

    @property
    def effect(self):
        return self._effect
    
    @property
    def color_mode(self):
        return self._color_mode

    @retry_bluetooth_connection_error
    async def set_rgb_color(self, rgb: Tuple[int, int, int], brightness: int | None = None):
        self._rgb_color = rgb
        if brightness is None:
            if self._brightness is None:
                self._brightness = 255
            else:
                brightness = self._brightness
        brightness_percent = int(brightness * 100 / 255)
        # Now adjust the RBG values to match the brightness
        red = int(rgb[0] * brightness_percent / 100)
        green = int(rgb[1] * brightness_percent / 100)
        blue = int(rgb[2] * brightness_percent / 100)
        # RGB packet
        rgb_packet = bytearray.fromhex("69 96 05 02")
        rgb_packet.append(red)
        rgb_packet.append(green)
        rgb_packet.append(blue)
        await self._write(rgb_packet)

    async def set_brightness_local(self, value: int):
        # 0 - 255, should convert automatically with the hex calls
        # call color temp or rgb functions to update
        self._brightness = value
        await self.set_rgb_color(self._rgb_color, value)

    @retry_bluetooth_connection_error
    async def turn_on(self):
        await self._write(self._turn_on_cmd)
        self._is_on = True
                
    @retry_bluetooth_connection_error
    async def turn_off(self):
        await self._write(self._turn_off_cmd)
        self._is_on = False

    @retry_bluetooth_connection_error
    async def set_effect(self, effect: str):
        if effect not in EFFECT_LIST:
            LOGGER.error("Effect %s not supported", effect)
            return
        self._effect = effect
        effect_packet = bytearray.fromhex("69 96 03 03")
        effect_id = EFFECT_MAP.get(effect)
        LOGGER.debug('Effect ID: %s', effect_id)
        LOGGER.debug('Effect name: %s', effect)
        effect_packet.append(effect_id)
        effect_packet.append(0x01) # FIXME: Need to support speed in here too, but can't because there isn't the ability in HA to specify it as far as I can tell.
        await self._write(effect_packet)

    @retry_bluetooth_connection_error
    async def turn_on(self):
        await self._write(self._turn_on_cmd)
        self._is_on = True

    @retry_bluetooth_connection_error
    async def turn_off(self):
        await self._write(self._turn_off_cmd)
        self._is_on = False

    @retry_bluetooth_connection_error
    async def update(self):
        LOGGER.debug("%s: Update in bjled called", self.name)
        # try:
        #     await self._ensure_connected()
        #     self._is_on = False
        #     self._brightness = 255
        #     self._rgb_color = (255,0,0)
        #     self._effect = None

        # except Exception as error:
        #     self._is_on = None # failed to connect, this should mark it as unavailable
        #     LOGGER.error("Error getting status: %s", error)
        #     track = traceback.format_exc()
        #     LOGGER.debug(track)

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete",
                self.name,
            )
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return
        async with self._connect_lock:
            # Check again while holding the lock
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return
            LOGGER.debug("%s: Connecting", self.name)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                self.name,
                self._disconnected,
                cached_services=self._cached_services,
                ble_device_callback=lambda: self._device,
            )
            LOGGER.debug("%s: Connected", self.name)
            resolved = self._resolve_characteristics(client.services)
            if not resolved:
                # Try to handle services failing to load
                #resolved = self._resolve_characteristics(await client.get_services())
                resolved = self._resolve_characteristics(client.services)
            self._cached_services = client.services if resolved else None

            self._client = client
            self._reset_disconnect_timer()

            # # Subscribe to notification is needed for LEDnetWF devices to accept commands
            # self._notification_callback = self._notification_handler
            # await client.start_notify(self._read_uuid, self._notification_callback)
            # LOGGER.debug("%s: Subscribed to notifications", self.name)
            
            # Send initial packets to device to see if it sends notifications
            # LOGGER.debug("%s: Send initial packets", self.name)
            # await self._write_while_connected(INITIAL_PACKET)

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve characteristics."""
        # for characteristic in NOTIFY_CHARACTERISTIC_UUIDS:
        #     if char := services.get_characteristic(characteristic):
        #         self._read_uuid = char
        #         LOGGER.debug("%s: Read UUID: %s", self.name, self._read_uuid)
        #         break
        for characteristic in WRITE_CHARACTERISTIC_UUIDS:
            if char := services.get_characteristic(characteristic):
                self._write_uuid = char
                break
        return bool(self._read_uuid and self._write_uuid)

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        if self._delay is not None and self._delay != 0:
            LOGGER.debug(
                "%s: Configured disconnect from device in %s seconds",
                self.name,
                self._delay
            )
            self._disconnect_timer = self.loop.call_later(self._delay, self._disconnect)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._expected_disconnect:
            LOGGER.debug("%s: Disconnected from device", self.name)
            return
        LOGGER.warning("%s: Device unexpectedly disconnected", self.name)

    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def stop(self) -> None:
        """Stop the LEDBLE."""
        LOGGER.debug("%s: Stop", self.name)
        await self._execute_disconnect()

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        LOGGER.debug(
            "%s: Disconnecting after timeout of %s",
            self.name,
            self._delay
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            read_char = self._read_uuid
            client = self._client
            self._expected_disconnect = True
            self._client = None
            self._write_uuid = None
            # self._read_uuid = None
            if client and client.is_connected:
                # await client.stop_notify(read_char) #  TODO:  I don't think this is needed.  Bleak docs say it isnt.
                await client.disconnect()
            LOGGER.debug("%s: Disconnected", self.name)
    
    def normalize_brightness(self, new_brightness):
        "Make sure brightness is between 2 and 255 and then convert to percentage"
        LOGGER.debug("Doing Normalizing brightness function")
        LOGGER.debug("New brightness passed IN is %s", new_brightness)
        if new_brightness is None and self._brightness is None:
            new_brightness = 255
        elif new_brightness is None and self._brightness > 1:
            new_brightness = self._brightness
        if new_brightness < 2:
            new_brightness = 2
        if new_brightness > 255:
            new_brightness = 255
        LOGGER.debug("New brightness (0-255) is %s", new_brightness)
        self._brightness = new_brightness
        new_percentage = int(new_brightness * 100 / 255)
        LOGGER.debug("Normalized brightness percent is %s", new_percentage)
        return new_percentage
    