"""Platform for light integration."""

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .cync_entity import CyncEntity


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
            and hub.cync_switches[switch_id].plug
            and switch_id in config_entry.options["switches"]
        ):
            new_devices.append(CyncPlugEntity(hub.cync_switches[switch_id]))

    if new_devices:
        async_add_entities(new_devices)


class CyncPlugEntity(SwitchEntity, CyncEntity):
    """Representation of a Cync Switch Entity."""

    _attr_should_poll: bool = False
    _type: str = "cync_switch_"

    @property
    def device_class(self) -> str | None:
        """Return the device class"""
        return SwitchDeviceClass.OUTLET
