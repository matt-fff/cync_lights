"""Platform for light integration."""

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .cync_entity import CyncEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][config_entry.entry_id]

    new_devices = []
    for room in hub.cync_rooms:
        if not hub.cync_rooms[room]._update_callback and (
            room in config_entry.options["rooms"]
            or room in config_entry.options["subgroups"]
        ):
            new_devices.append(CyncRoomEntity(hub.cync_rooms[room]))

    for switch_id in hub.cync_switches:
        if (
            not hub.cync_switches[switch_id]._update_callback
            and not hub.cync_switches[switch_id].plug
            and not hub.cync_switches[switch_id].fan
            and switch_id in config_entry.options["switches"]
        ):
            new_devices.append(CyncSwitchEntity(hub.cync_switches[switch_id]))

    if new_devices:
        async_add_entities(new_devices)


class CyncBaseLightEntity(LightEntity, CyncEntity):
    @property
    def brightness(self) -> int | None:
        """Return the brightness of this room between 0..255."""
        return round(self.entity.brightness * 255 / 100)

    @property
    def max_mireds(self) -> int:
        """Return minimum supported color temperature."""
        return self.entity.max_mireds

    @property
    def min_mireds(self) -> int:
        """Return maximum supported color temperature."""
        return self.entity.min_mireds

    @property
    def color_temp(self) -> int | None:
        """Return the color temperature of this light in mireds for HA."""
        return self.max_mireds - round(
            (self.max_mireds - self.min_mireds) * self.entity.color_temp / 100
        )

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the RGB color tuple of this light switch"""
        return (
            self.entity.rgb["r"],
            self.entity.rgb["g"],
            self.entity.rgb["b"],
        )

    @property
    def supported_color_modes(self) -> set[str] | None:
        """Return list of available color modes."""

        modes: set[ColorMode | str] = set()

        if self.entity.support_color_temp:
            modes.add(ColorMode.COLOR_TEMP)
        if self.entity.support_rgb:
            modes.add(ColorMode.RGB)
        if self.entity.support_brightness:
            modes.add(ColorMode.BRIGHTNESS)
        if not modes:
            modes.add(ColorMode.ONOFF)

        return modes

    @property
    def color_mode(self) -> str | None:
        """Return the active color mode."""

        if self.entity.support_color_temp:
            if self.entity.support_rgb and self.entity.rgb["active"]:
                return ColorMode.RGB
            return ColorMode.COLOR_TEMP
        if self.entity.support_brightness:
            return ColorMode.BRIGHTNESS
        return ColorMode.ONOFF

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light."""
        await self.entity.turn_on(
            kwargs.get(ATTR_RGB_COLOR),
            kwargs.get(ATTR_BRIGHTNESS),
            kwargs.get(ATTR_COLOR_TEMP),
        )


class CyncSwitchEntity(CyncBaseLightEntity):
    """Representation of a Cync Switch Light Entity."""

    _type: str = "cync_switch_"


class CyncRoomEntity(CyncBaseLightEntity):
    """Representation of a Cync Room Light Entity."""

    _type: str = "cync_room_"

    @property
    def device_name(self) -> str:
        if self.entity.is_subgroup:
            return self.entity.parent_room
        return f"{self.entity.name} ({self.entity.home_name})"

    @property
    def area(self) -> str:
        if self.entity.is_subgroup:
            return self.entity.parent_room

        return self.entity.name

    @property
    def icon(self) -> str | None:
        """Icon of the entity."""
        if self.entity.is_subgroup:
            return "mdi:lightbulb-group-outline"
        return "mdi:lightbulb-group"

    @property
    def unique_id(self) -> str:
        """Return Unique ID string."""
        # TODO will this cause room IDs to change
        # when their contents change? Seems wrong.
        uid = (
            self._type
            + "-".join(self.entity.switches)
            + "_"
            + "-".join(self.entity.subgroups)
        )
        return uid
