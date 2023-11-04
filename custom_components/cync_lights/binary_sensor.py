"""Platform for binary sensor integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    for sensor in hub.cync_motion_sensors:
        if (
            not hub.cync_motion_sensors[sensor]._update_callback
            and sensor in config_entry.options["motion_sensors"]
        ):
            new_devices.append(
                CyncMotionSensorEntity(hub.cync_motion_sensors[sensor])
            )
    for sensor in hub.cync_ambient_light_sensors:
        if (
            not hub.cync_ambient_light_sensors[sensor]._update_callback
            and sensor in config_entry.options["ambient_light_sensors"]
        ):
            new_devices.append(
                CyncAmbientLightSensorEntity(
                    hub.cync_ambient_light_sensors[sensor]
                )
            )

    if new_devices:
        async_add_entities(new_devices)


class CyncMotionSensorEntity(BinarySensorEntity, CyncEntity):
    """Representation of a Cync Motion Sensor."""

    type_: str = "cync_motion_sensor_"

    @property
    def name(self) -> str:
        """Return the name of the motion_sensor."""
        return self.entity.name + " Motion"

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self.entity.motion

    @property
    def device_class(self) -> str | None:
        """Return the device class"""
        return BinarySensorDeviceClass.MOTION


class CyncAmbientLightSensorEntity(BinarySensorEntity, CyncEntity):
    """Representation of a Cync Ambient Light Sensor."""

    type_: str = "cync_ambient_light_sensor_"

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self.entity.name + " Ambient Light"

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self.entity.ambient_light

    @property
    def device_class(self) -> str | None:
        """Return the device class"""
        return BinarySensorDeviceClass.LIGHT
