from typing import Any

from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN


class CyncEntity(Entity):
    """Representation of a Cync Base Entity."""

    _attr_should_poll: bool = False
    _type: str = "cync_entity_"

    def __init__(self, entity) -> None:
        """Initialize the entity."""
        self.entity = entity

    async def async_added_to_hass(self) -> None:
        """Run when this Entity has been added to HA."""
        self.entity.register(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Entity being removed from hass."""
        self.entity.reset()

    @property
    def device_name(self) -> str:
        return self.entity.room.name + f" ({self.entity.home_name})"

    @property
    def area(self) -> str:
        return self.entity.room.name

    @property
    def device_info(self) -> DeviceInfo:
        """Return entity registry information for this entity."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_name)},
            manufacturer="Cync by Savant",
            name=self.device_name,
            suggested_area=self.area,
        )

    @property
    def unique_id(self) -> str:
        """Return Unique ID string."""
        return self._type + self.entity.device_id

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self.entity.name

    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self.entity.power_state

    async def async_turn_on(self, **_: Any) -> None:
        """Turn on the outlet."""
        await self.entity.turn_on(None, None, None)

    async def async_turn_off(self, **_: Any) -> None:
        """Turn off the entity."""
        await self.entity.turn_off()
