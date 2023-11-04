"""Platform for light integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
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
    for switch_id in hub.cync_switches:
        if (
            not hub.cync_switches[switch_id]._update_callback
            and hub.cync_switches[switch_id].fan
            and switch_id in config_entry.options["switches"]
        ):
            new_devices.append(CyncFanEntity(hub.cync_switches[switch_id]))

    if new_devices:
        async_add_entities(new_devices)


class CyncFanEntity(FanEntity, CyncEntity):
    """Representation of a Cync Fan Switch Entity."""

    type_: str = "cync_switch_"

    @property
    def supported_features(self) -> int:
        """Return true if fan is on."""
        return FanEntityFeature.SET_SPEED

    @property
    def percentage(self) -> int | None:
        """Return the fan speed percentage of this switch"""
        return self.entity.brightness

    @property
    def speed_count(self) -> int:
        """Return the number of speeds the fan supports."""
        # TODO I'm guessing this is a placeholder
        return 4

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the light."""
        await self.entity.turn_on(
            None,
            percentage * 255 / 100 if percentage is not None else None,
            None,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self.entity.turn_off()

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed of the fan, as a percentage."""
        if percentage == 0:
            await self.async_turn_off()
        else:
            await self.entity.turn_on(None, percentage * 255 / 100, None)
