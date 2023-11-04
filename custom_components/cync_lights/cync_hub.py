import asyncio
import logging
import math
import ssl
import struct
import threading
from typing import Any

import aiohttp

from .const import (
    API_2FACTOR_AUTH,
    API_AUTH,
    API_DEVICE_INFO,
    API_DEVICES,
    API_REQUEST_CODE,
    CAPABILITIES,
)

_LOGGER = logging.getLogger(__name__)


class CyncHub:
    def __init__(self, user_data, options, remove_options_update_listener):
        self.thread = None
        self.loop = None
        self.reader = None
        self.writer = None
        self.login_code = bytearray(user_data["cync_credentials"])
        self.logged_in = False
        self.home_devices = user_data["cync_config"]["home_devices"]
        self.home_controllers = user_data["cync_config"]["home_controllers"]
        self.switch_to_home_id_map = user_data["cync_config"][
            "switchID_to_homeID"
        ]
        self.connected_devices = {
            home_id: [] for home_id in self.home_controllers.keys()
        }
        self.shutting_down = False
        self.remove_options_update_listener = remove_options_update_listener
        self.cync_rooms = {
            room_id: CyncRoom(room_id, room_info, self)
            for room_id, room_info in user_data["cync_config"]["rooms"].items()
        }
        self.cync_switches = {
            device_id: CyncSwitch(
                device_id,
                switch_info,
                self.cync_rooms.get(switch_info["room"], None),
                self,
            )
            for device_id, switch_info in user_data["cync_config"][
                "devices"
            ].items()
            if switch_info.get("ONOFF", False)
        }
        self.cync_motion_sensors = {
            device_id: CyncMotionSensor(
                device_id,
                device_info,
                self.cync_rooms.get(device_info["room"], None),
            )
            for device_id, device_info in user_data["cync_config"][
                "devices"
            ].items()
            if device_info.get("MOTION", False)
        }
        self.cync_ambient_light_sensors = {
            device_id: CyncAmbientLightSensor(
                device_id,
                device_info,
                self.cync_rooms.get(device_info["room"], None),
            )
            for device_id, device_info in user_data["cync_config"][
                "devices"
            ].items()
            if device_info.get("AMBIENT_LIGHT", False)
        }
        self.switch_to_device_id_map = {
            device_info.switch_id: [
                dev_id
                for dev_id, dev_info in self.cync_switches.items()
                if dev_info.switch_id == device_info.switch_id
            ]
            for device_id, device_info in self.cync_switches.items()
            if int(device_info.switch_id) > 0
        }
        self.connected_devices_updated = False
        self.options = options
        self._seq_num = 0
        self.pending_commands = {}
        _ = [room.initialize() for room in self.cync_rooms.values()]

    def start_tcp_client(self):
        self.thread = threading.Thread(
            target=self._start_tcp_client, daemon=True
        )
        self.thread.start()

    def _start_tcp_client(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect())

    def disconnect(self):
        self.shutting_down = True
        for (
            home_controllers
        ) in (
            self.home_controllers.values()
        ):  # send packets to server to generate data to be
            # read which will initiate shutdown
            for controller in home_controllers:
                seq = self.get_seq_num()
                state_request = (
                    bytes.fromhex("7300000018")
                    + int(controller).to_bytes(4, "big")
                    + seq.to_bytes(2, "big")
                    + bytes.fromhex("007e00000000f85206000000ffff0000567e")
                )

                self.loop.call_soon_threadsafe(
                    self.send_request, state_request
                )

    async def _connect(self):
        uri = "cm.gelighting.com"
        port = 23778
        ssl_port = 23779

        while not self.shutting_down:
            try:
                context = ssl.create_default_context()
                try:
                    self.reader, self.writer = await asyncio.open_connection(
                        uri, ssl_port, ssl=context
                    )
                except Exception as exc:
                    _LOGGER.warning(
                        "%s: %s", str(type(exc).__name__), str(exc)
                    )
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    try:
                        self.reader, self.writer = (
                            await asyncio.open_connection(
                                uri, ssl_port, ssl=context
                            )
                        )
                    except Exception as exc:
                        _LOGGER.warning(
                            "%s: %s", str(type(exc).__name__), str(exc)
                        )
                        self.reader, self.writer = (
                            await asyncio.open_connection(uri, port)
                        )
            except Exception as exc:
                _LOGGER.error("%s: %s", str(type(exc).__name__), str(exc))
                await asyncio.sleep(5)
            else:
                read_tcp_messages = asyncio.create_task(
                    self._read_tcp_messages(), name="Read TCP Messages"
                )
                maintain_connection = asyncio.create_task(
                    self._maintain_connection(), name="Maintain Connection"
                )
                update_state = asyncio.create_task(
                    self._update_state(), name="Update State"
                )
                update_connected_devices = asyncio.create_task(
                    self._update_connected_devices(),
                    name="Update Connected Devices",
                )
                read_write_tasks = [
                    read_tcp_messages,
                    maintain_connection,
                    update_state,
                    update_connected_devices,
                ]
                try:
                    done, pending = await asyncio.wait(
                        read_write_tasks, return_when=asyncio.FIRST_EXCEPTION
                    )
                    for task in done:
                        name = task.get_name()
                        exception = task.exception()
                        if exception is not None:
                            _LOGGER.error(
                                "Encountered an error in task '%s' - %s: %s",
                                str(name),
                                str(type(exception).__name__),
                                str(exception),
                            )

                        try:
                            # TODO should we use this result?
                            task.result()
                        except Exception as exc:
                            _LOGGER.error(
                                "%s: %s", str(type(exc).__name__), str(exc)
                            )
                    for task in pending:
                        task.cancel()
                    if not self.shutting_down:
                        _LOGGER.error(
                            "Connection to Cync server reset, restarting in 15"
                            " seconds"
                        )
                        await asyncio.sleep(15)
                    else:
                        _LOGGER.debug("Cync client shutting down")
                except Exception as exc:
                    _LOGGER.error("%s: %s", str(type(exc).__name__), str(exc))

    async def _read_tcp_messages(self):
        self.writer.write(self.login_code)
        await self.writer.drain()
        await self.reader.read(1000)
        self.logged_in = True
        while not self.shutting_down:
            data = await self.reader.read(1000)
            if len(data) == 0:
                self.logged_in = False
                raise LostConnection

            while len(data) >= 12:
                packet_type = int(data[0])
                packet_length = struct.unpack(">I", data[1:5])[0]
                packet = data[5 : packet_length + 5]
                try:
                    if packet_length == len(packet):
                        if packet_type == 115:
                            await self._process_tcp_115(packet)
                        elif packet_type == 131:
                            await self._process_tcp_131(packet)
                        elif (
                            packet_type == 67
                            and packet_length >= 26
                            and int(packet[4]) == 1
                            and int(packet[5]) == 1
                            and int(packet[6]) == 6
                        ):
                            await self._process_tcp_67(packet)
                        elif packet_type == 171:
                            await self._process_tcp_171(packet)
                        elif packet_type == 123:
                            await self._process_tcp_123(packet)
                except Exception as exc:
                    _LOGGER.error("%s: %s", str(type(exc).__name__), str(exc))
                data = data[packet_length + 5 :]

        raise ShuttingDown

    async def _process_tcp_115(self, packet):
        # Implementation for packet_type 115
        packet_length = len(packet)
        switch_id = str(struct.unpack(">I", packet[0:4])[0])
        home_id = self.switch_to_home_id_map[switch_id]

        # send response packet
        response_id = struct.unpack(">H", packet[4:6])[0]
        response_packet = (
            bytes.fromhex("7300000007")
            + int(switch_id).to_bytes(4, "big")
            + response_id.to_bytes(2, "big")
            + bytes.fromhex("00")
        )
        self.loop.call_soon_threadsafe(self.send_request, response_packet)

        if packet_length >= 33 and int(packet[13]) == 219:
            # parse state and brightness change packet
            device_id = self.home_devices[home_id][int(packet[21])]
            state = int(packet[27]) > 0
            brightness = int(packet[28]) if state else 0
            if device_id in self.cync_switches:
                self.cync_switches[device_id].update_switch(
                    state,
                    brightness,
                    self.cync_switches[device_id].color_temp,
                    self.cync_switches[device_id].rgb,
                )
        elif packet_length >= 25 and int(packet[13]) == 84:
            # parse motion and ambient light sensor packet
            device_id = self.home_devices[home_id][int(packet[16])]
            motion = int(packet[22]) > 0
            ambient_light = int(packet[24]) > 0
            if device_id in self.cync_motion_sensors:
                self.cync_motion_sensors[device_id].update_motion_sensor(
                    motion
                )
            if device_id in self.cync_ambient_light_sensors:
                self.cync_ambient_light_sensors[
                    device_id
                ].update_ambient_light_sensor(ambient_light)
        elif packet_length > 51 and int(packet[13]) == 82:
            # parse initial state packet
            switch_id = str(struct.unpack(">I", packet[0:4])[0])
            home_id = self.switch_to_home_id_map[switch_id]
            self._add_connected_devices(switch_id, home_id)
            packet = packet[22:]
            while len(packet) > 24:
                device_id = self.home_devices[home_id][int(packet[0])]
                if device_id in self.cync_switches:
                    if self.cync_switches[device_id].elements > 1:
                        for i in range(self.cync_switches[device_id].elements):
                            device_id = self.home_devices[home_id][
                                (i + 1) * 256 + int(packet[0])
                            ]
                            state = (
                                int((int(packet[12]) >> i) & int(packet[8]))
                                > 0
                            )
                            brightness = 100 if state else 0
                            self.cync_switches[device_id].update_switch(
                                state,
                                brightness,
                                self.cync_switches[device_id].color_temp,
                                self.cync_switches[device_id].rgb,
                            )
                    else:
                        state = int(packet[8]) > 0
                        brightness = int(packet[12]) if state else 0
                        color_temp = int(packet[16])
                        rgb = {
                            "r": int(packet[20]),
                            "g": int(packet[21]),
                            "b": int(packet[22]),
                            "active": int(packet[16]) == 254,
                        }
                        self.cync_switches[device_id].update_switch(
                            state, brightness, color_temp, rgb
                        )
                packet = packet[24:]

    async def _process_tcp_131(self, packet):
        # Implementation for packet_type 131
        packet_length = len(packet)
        switch_id = str(struct.unpack(">I", packet[0:4])[0])
        home_id = self.switch_to_home_id_map[switch_id]
        if packet_length >= 33 and int(packet[13]) == 219:
            # parse state and brightness change packet for packet_type 131
            device_id = self.home_devices[home_id][int(packet[21])]
            state = int(packet[27]) > 0
            brightness = int(packet[28]) if state else 0
            if device_id in self.cync_switches:
                self.cync_switches[device_id].update_switch(
                    state,
                    brightness,
                    self.cync_switches[device_id].color_temp,
                    self.cync_switches[device_id].rgb,
                )
        elif packet_length >= 25 and int(packet[13]) == 84:
            # parse motion and ambient light sensor packet for packet_type 131
            device_id = self.home_devices[home_id][int(packet[16])]
            motion = int(packet[22]) > 0
            ambient_light = int(packet[24]) > 0
            if device_id in self.cync_motion_sensors:
                self.cync_motion_sensors[device_id].update_motion_sensor(
                    motion
                )
            if device_id in self.cync_ambient_light_sensors:
                self.cync_ambient_light_sensors[
                    device_id
                ].update_ambient_light_sensor(ambient_light)

    async def _process_tcp_67(self, packet):
        # Implementation for packet_type 67
        switch_id = str(struct.unpack(">I", packet[0:4])[0])
        home_id = self.switch_to_home_id_map[switch_id]
        # parse state packet for packet_type 67
        packet = packet[7:]
        while len(packet) >= 19:
            if int(packet[3]) < len(self.home_devices[home_id]):
                device_id = self.home_devices[home_id][int(packet[3])]
                if device_id in self.cync_switches:
                    if self.cync_switches[device_id].elements > 1:
                        for i in range(self.cync_switches[device_id].elements):
                            device_id = self.home_devices[home_id][
                                (i + 1) * 256 + int(packet[3])
                            ]
                            state = (
                                int((int(packet[5]) >> i) & int(packet[4])) > 0
                            )
                            brightness = 100 if state else 0
                            self.cync_switches[device_id].update_switch(
                                state,
                                brightness,
                                self.cync_switches[device_id].color_temp,
                                self.cync_switches[device_id].rgb,
                            )
                    else:
                        state = int(packet[4]) > 0
                        brightness = int(packet[5]) if state else 0

    async def _process_tcp_171(self, packet):
        # Implementation for packet_type 171
        switch_id = str(struct.unpack(">I", packet[0:4])[0])
        home_id = self.switch_to_home_id_map[switch_id]
        self._add_connected_devices(switch_id, home_id)

    async def _process_tcp_123(self, packet):
        # Implementation for packet_type 123
        seq = str(struct.unpack(">H", packet[4:6])[0])
        command_received = self.pending_commands.get(seq, None)
        if command_received is not None:
            command_received(seq)

    async def _maintain_connection(self):
        while not self.shutting_down:
            await asyncio.sleep(180)
            self.writer.write(bytes.fromhex("d300000000"))
            await self.writer.drain()
        raise ShuttingDown

    def _add_connected_devices(self, switch_id, home_id):
        for dev in self.switch_to_device_id_map[switch_id]:
            # update list of WiFi connected devices
            if dev not in self.connected_devices[home_id]:
                self.connected_devices[home_id].append(dev)
                if self.connected_devices_updated:
                    for dev in self.cync_switches.values():
                        dev.update_controllers()
                    for room in self.cync_rooms.values():
                        room.update_controllers()

    async def _update_connected_devices(self):
        while not self.shutting_down:
            self.connected_devices_updated = False
            for devices in self.connected_devices.values():
                devices.clear()
            while not self.logged_in:
                await asyncio.sleep(2)
            attempts = 0
            while (
                True
                in [
                    len(devices) < len(self.home_controllers[home_id]) * 0.5
                    for home_id, devices in self.connected_devices.items()
                ]
                and attempts < 10
            ):
                for home_controllers in self.home_controllers.values():
                    for controller in home_controllers:
                        seq = self.get_seq_num()
                        ping = (
                            bytes.fromhex("a300000007")
                            + int(controller).to_bytes(4, "big")
                            + seq.to_bytes(2, "big")
                            + bytes.fromhex("00")
                        )
                        self.loop.call_soon_threadsafe(self.send_request, ping)
                        await asyncio.sleep(0.15)
                await asyncio.sleep(2)
                attempts += 1
            for dev in self.cync_switches.values():
                dev.update_controllers()
            for room in self.cync_rooms.values():
                room.update_controllers()
            self.connected_devices_updated = True
            await asyncio.sleep(3600)
        raise ShuttingDown

    async def _update_state(self):
        while not self.connected_devices_updated:
            await asyncio.sleep(2)
        for connected_devices in self.connected_devices.values():
            if len(connected_devices) > 0:
                controller = self.cync_switches[connected_devices[0]].switch_id
                seq = self.get_seq_num()
                state_request = (
                    bytes.fromhex("7300000018")
                    + int(controller).to_bytes(4, "big")
                    + seq.to_bytes(2, "big")
                    + bytes.fromhex("007e00000000f85206000000ffff0000567e")
                )
                self.loop.call_soon_threadsafe(
                    self.send_request, state_request
                )
        while False in [
            self.cync_switches[dev_id]._update_callback is not None
            for dev_id in self.options["switches"]
        ] and False in [
            self.cync_rooms[dev_id]._update_callback is not None
            for dev_id in self.options["rooms"]
        ]:
            await asyncio.sleep(2)
        for dev in self.cync_switches.values():
            dev.publish_update()
        for room in self.cync_rooms.values():
            room.publish_update()

    def send_request(self, request):
        async def send():
            self.writer.write(request)
            await self.writer.drain()

        self.loop.create_task(send())

    def combo_control(
        self, state, brightness, color_tone, rgb, switch_id, mesh_id, seq
    ):
        combo_request = (
            bytes.fromhex("7300000022")
            + int(switch_id).to_bytes(4, "big")
            + int(seq).to_bytes(2, "big")
            + bytes.fromhex("007e00000000f8f010000000000000")
            + mesh_id
            + bytes.fromhex("f00000")
            + (1 if state else 0).to_bytes(1, "big")
            + brightness.to_bytes(1, "big")
            + color_tone.to_bytes(1, "big")
            + rgb[0].to_bytes(1, "big")
            + rgb[1].to_bytes(1, "big")
            + rgb[2].to_bytes(1, "big")
            + (
                (
                    496
                    + int(mesh_id[0])
                    + int(mesh_id[1])
                    + (1 if state else 0)
                    + brightness
                    + color_tone
                    + sum(rgb)
                )
                % 256
            ).to_bytes(1, "big")
            + bytes.fromhex("7e")
        )
        self.loop.call_soon_threadsafe(self.send_request, combo_request)

    def turn_on(self, switch_id, mesh_id, seq):
        power_request = (
            bytes.fromhex("730000001f")
            + int(switch_id).to_bytes(4, "big")
            + int(seq).to_bytes(2, "big")
            + bytes.fromhex("007e00000000f8d00d000000000000")
            + mesh_id
            + bytes.fromhex("d00000010000")
            + ((430 + int(mesh_id[0]) + int(mesh_id[1])) % 256).to_bytes(
                1, "big"
            )
            + bytes.fromhex("7e")
        )
        self.loop.call_soon_threadsafe(self.send_request, power_request)

    def turn_off(self, switch_id, mesh_id, seq):
        power_request = (
            bytes.fromhex("730000001f")
            + int(switch_id).to_bytes(4, "big")
            + int(seq).to_bytes(2, "big")
            + bytes.fromhex("007e00000000f8d00d000000000000")
            + mesh_id
            + bytes.fromhex("d00000000000")
            + ((429 + int(mesh_id[0]) + int(mesh_id[1])) % 256).to_bytes(
                1, "big"
            )
            + bytes.fromhex("7e")
        )
        self.loop.call_soon_threadsafe(self.send_request, power_request)

    def set_color_temp(self, color_temp, switch_id, mesh_id, seq):
        color_temp_request = (
            bytes.fromhex("730000001e")
            + int(switch_id).to_bytes(4, "big")
            + int(seq).to_bytes(2, "big")
            + bytes.fromhex("007e00000000f8e20c000000000000")
            + mesh_id
            + bytes.fromhex("e2000005")
            + color_temp.to_bytes(1, "big")
            + (
                (469 + int(mesh_id[0]) + int(mesh_id[1]) + color_temp) % 256
            ).to_bytes(1, "big")
            + bytes.fromhex("7e")
        )
        self.loop.call_soon_threadsafe(self.send_request, color_temp_request)

    def get_seq_num(self):
        if self._seq_num == 65535:
            self._seq_num = 1
        else:
            self._seq_num += 1
        return self._seq_num


class CyncRoom:
    def __init__(self, room_id, room_info, hub):
        self.hub = hub
        self.room_id = room_id
        self.home_id = room_id.split("-")[0]
        self.name = room_info.get("name", "unknown")
        self.home_name = room_info.get("home_name", "unknown")
        self.parent_room = room_info.get("parent_room", "unknown")
        self.mesh_id = int(room_info.get("mesh_id", 0)).to_bytes(2, "little")
        self.power_state = False
        self.brightness = 0
        self.color_temp = 0
        self.rgb = {"r": 0, "g": 0, "b": 0, "active": False}
        self.switches = room_info.get("switches", [])
        self.subgroups = room_info.get("subgroups", [])
        self.is_subgroup = room_info.get("isSubgroup", False)
        self.all_room_switches = self.switches
        self.controllers = []
        self.default_controller = room_info.get(
            "room_controller", self.hub.home_controllers[self.home_id][0]
        )
        self._update_callback = None
        self._update_parent_room = None
        self.support_brightness = False
        self.support_color_temp = False
        self.support_rgb = False
        self.switches_support_brightness = False
        self.switches_support_color_temp = False
        self.switches_support_rgb = False
        self.groups_support_brightness = False
        self.groups_support_color_temp = False
        self.groups_support_rgb = False
        self._command_timout = 0.5
        self._command_retry_time = 5

    def initialize(self):
        """
        Initialization of supported features
        and registration of update function for
        all switches and subgroups in the room
        """
        self.switches_support_brightness = [
            device_id
            for device_id in self.switches
            if self.hub.cync_switches[device_id].support_brightness
        ]
        self.switches_support_color_temp = [
            device_id
            for device_id in self.switches
            if self.hub.cync_switches[device_id].support_color_temp
        ]
        self.switches_support_rgb = [
            device_id
            for device_id in self.switches
            if self.hub.cync_switches[device_id].support_rgb
        ]
        self.groups_support_brightness = [
            room_id
            for room_id in self.subgroups
            if self.hub.cync_rooms[room_id].support_brightness
        ]
        self.groups_support_color_temp = [
            room_id
            for room_id in self.subgroups
            if self.hub.cync_rooms[room_id].support_color_temp
        ]
        self.groups_support_rgb = [
            room_id
            for room_id in self.subgroups
            if self.hub.cync_rooms[room_id].support_rgb
        ]
        self.support_brightness = (
            len(self.switches_support_brightness)
            + len(self.groups_support_brightness)
        ) > 0
        self.support_color_temp = (
            len(self.switches_support_color_temp)
            + len(self.groups_support_color_temp)
        ) > 0
        self.support_rgb = (
            len(self.switches_support_rgb) + len(self.groups_support_rgb)
        ) > 0
        for switch_id in self.switches:
            self.hub.cync_switches[switch_id].register_room_updater(
                self.update_room
            )
        for subgroup in self.subgroups:
            self.hub.cync_rooms[subgroup].register_room_updater(
                self.update_room
            )
            self.all_room_switches = (
                self.all_room_switches + self.hub.cync_rooms[subgroup].switches
            )
        for subgroup in self.subgroups:
            self.hub.cync_rooms[subgroup].all_room_switches = (
                self.all_room_switches
            )

    def register(self, update_callback) -> None:
        """Register callback, called when switch changes state."""
        self._update_callback = update_callback

    def reset(self) -> None:
        """Remove previously registered callback."""
        self._update_callback = None

    def register_room_updater(self, parent_updater):
        self._update_parent_room = parent_updater

    @property
    def max_mireds(self) -> int:
        """Return minimum supported color temperature."""
        return 500

    @property
    def min_mireds(self) -> int:
        """Return maximum supported color temperature."""
        return 200

    async def turn_on(self, attr_rgb, attr_br, attr_ct) -> None:
        """Turn on the light."""
        attempts = 0
        update_received = False
        while not update_received and attempts < int(
            self._command_retry_time / self._command_timout
        ):
            seq = str(self.hub.get_seq_num())
            if len(self.controllers) > 0:
                controller = self.controllers[attempts % len(self.controllers)]
            else:
                controller = self.default_controller
            if attr_rgb is not None and attr_br is not None:
                if math.isclose(
                    attr_br,
                    max([self.rgb["r"], self.rgb["g"], self.rgb["b"]])
                    * self.brightness
                    / 100,
                    abs_tol=2,
                ):
                    self.hub.combo_control(
                        True,
                        self.brightness,
                        254,
                        attr_rgb,
                        controller,
                        self.mesh_id,
                        seq,
                    )
                else:
                    self.hub.combo_control(
                        True,
                        round(attr_br * 100 / 255),
                        255,
                        [255, 255, 255],
                        controller,
                        self.mesh_id,
                        seq,
                    )
            elif attr_rgb is None and attr_ct is None and attr_br is not None:
                self.hub.combo_control(
                    True,
                    round(attr_br * 100 / 255),
                    255,
                    [255, 255, 255],
                    controller,
                    self.mesh_id,
                    seq,
                )
            elif attr_rgb is not None and attr_br is None:
                self.hub.combo_control(
                    True,
                    self.brightness,
                    254,
                    attr_rgb,
                    controller,
                    self.mesh_id,
                    seq,
                )
            elif attr_ct is not None:
                ct = round(
                    100
                    * (self.max_mireds - attr_ct)
                    / (self.max_mireds - self.min_mireds)
                )
                self.hub.set_color_temp(ct, controller, self.mesh_id, seq)
            else:
                self.hub.turn_on(controller, self.mesh_id, seq)
            self.hub.pending_commands[seq] = self.command_received
            await asyncio.sleep(self._command_timout)
            if self.hub.pending_commands.get(seq, None) is not None:
                self.hub.pending_commands.pop(seq)
                attempts += 1
            else:
                update_received = True

    async def turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        attempts = 0
        update_received = False
        while not update_received and attempts < int(
            self._command_retry_time / self._command_timout
        ):
            seq = str(self.hub.get_seq_num())
            if len(self.controllers) > 0:
                controller = self.controllers[attempts % len(self.controllers)]
            else:
                controller = self.default_controller
            self.hub.turn_off(controller, self.mesh_id, seq)
            self.hub.pending_commands[seq] = self.command_received
            await asyncio.sleep(self._command_timout)
            if self.hub.pending_commands.get(seq, None) is not None:
                self.hub.pending_commands.pop(seq)
                attempts += 1
            else:
                update_received = True

    def command_received(self, seq):
        """
        Remove command from hub.pending_commands
        when a reply is received from Cync server
        """
        if self.hub.pending_commands.get(seq) is not None:
            self.hub.pending_commands.pop(seq)

    def update_room(self):
        """Update the current state of the room"""
        _brightness = self.brightness
        _color_temp = self.color_temp
        _rgb = self.rgb
        _power_state = True in (
            [
                self.hub.cync_switches[device_id].power_state
                for device_id in self.switches
            ]
            + [
                self.hub.cync_rooms[room_id].power_state
                for room_id in self.subgroups
            ]
        )
        if self.support_brightness:
            _brightness = round(
                sum(
                    [
                        self.hub.cync_switches[device_id].brightness
                        for device_id in self.switches
                    ]
                    + [
                        self.hub.cync_rooms[room_id].brightness
                        for room_id in self.subgroups
                    ]
                )
                / (len(self.switches) + len(self.subgroups))
            )
        else:
            _brightness = 100 if _power_state else 0
        if self.support_color_temp:
            color_temps = [
                self.hub.cync_switches[device_id].color_temp
                for device_id in self.switches
                if self.switches_support_color_temp
            ] + [
                self.hub.cync_rooms[room_id].color_temp
                for room_id in self.subgroups
                if self.groups_support_color_temp
            ]

            _color_temp = round(sum(color_temps) / len(color_temps))
        if self.support_rgb:

            for key in ("r", "g", "b"):
                values = [
                    self.hub.cync_switches[device_id].rgb[key]
                    for device_id in self.switches
                    if self.switches_support_rgb
                ] + [
                    self.hub.cync_rooms[room_id].rgb[key]
                    for room_id in self.subgroups
                    if self.groups_support_rgb
                ]
                _rgb[key] = round(sum(values) / (len(values)))

            _rgb["active"] = any(
                [
                    self.hub.cync_switches[device_id].rgb["active"]
                    for device_id in self.switches
                    if self.switches_support_rgb
                ]
                + [
                    self.hub.cync_rooms[room_id].rgb["active"]
                    for room_id in self.subgroups
                    if self.groups_support_rgb
                ]
            )

        if (
            _power_state != self.power_state
            or _brightness != self.brightness
            or _color_temp != self.color_temp
            or _rgb != self.rgb
        ):
            self.power_state = _power_state
            self.brightness = _brightness
            self.color_temp = _color_temp
            self.rgb = _rgb
            self.publish_update()
            if self._update_parent_room:
                self._update_parent_room()

    def update_controllers(self):
        """Update the list of responsive, Wi-Fi connected controller devices"""
        connected_devices = self.hub.connected_devices[self.home_id]
        controllers = []
        if len(connected_devices) > 0:
            controllers = [
                self.hub.cync_switches[dev_id].switch_id
                for dev_id in self.all_room_switches
                if dev_id in connected_devices
            ]
            others_available = [
                self.hub.cync_switches[dev_id].switch_id
                for dev_id in connected_devices
            ]
            for controller in controllers:
                if controller in others_available:
                    others_available.remove(controller)
            self.controllers = controllers + others_available
        else:
            self.controllers = [self.default_controller]

    def publish_update(self):
        if self._update_callback:
            self._update_callback()


class CyncSwitch:
    def __init__(self, device_id, switch_info, room, hub):
        self.hub = hub
        self.device_id = device_id
        self.switch_id = switch_info.get("switch_id", "0")
        self.home_id = [
            home_id
            for home_id, home_devices in self.hub.home_devices.items()
            if self.device_id in home_devices
        ][0]
        self.name = switch_info.get("name", "unknown")
        self.home_name = switch_info.get("home_name", "unknown")
        self.mesh_id = switch_info.get("mesh_id", 0).to_bytes(2, "little")
        self.room = room
        self.power_state = False
        self.brightness = 0
        self.color_temp = 0
        self.rgb = {"r": 0, "g": 0, "b": 0, "active": False}
        self.default_controller = switch_info.get(
            "switch_controller", self.hub.home_controllers[self.home_id][0]
        )
        self.controllers = []
        self._update_callback = None
        self._update_parent_room = None
        self.support_brightness = switch_info.get("BRIGHTNESS", False)
        self.support_color_temp = switch_info.get("COLORTEMP", False)
        self.support_rgb = switch_info.get("RGB", False)
        self.plug = switch_info.get("PLUG", False)
        self.fan = switch_info.get("FAN", False)
        self.elements = switch_info.get("MULTIELEMENT", 1)
        self._command_timout = 0.5
        self._command_retry_time = 5

    def register(self, update_callback) -> None:
        """Register callback, called when switch changes state."""
        self._update_callback = update_callback

    def reset(self) -> None:
        """Remove previously registered callback."""
        self._update_callback = None

    def register_room_updater(self, parent_updater):
        self._update_parent_room = parent_updater

    @property
    def max_mireds(self) -> int:
        """Return minimum supported color temperature."""
        return 500

    @property
    def min_mireds(self) -> int:
        """Return maximum supported color temperature."""
        return 200

    async def turn_on(self, attr_rgb, attr_br, attr_ct) -> None:
        """Turn on the light."""
        attempts = 0
        update_received = False
        while not update_received and attempts < int(
            self._command_retry_time / self._command_timout
        ):
            seq = str(self.hub.get_seq_num())
            if len(self.controllers) > 0:
                controller = self.controllers[attempts % len(self.controllers)]
            else:
                controller = self.default_controller
            if attr_rgb is not None and attr_br is not None:
                if math.isclose(
                    attr_br,
                    max([self.rgb["r"], self.rgb["g"], self.rgb["b"]])
                    * self.brightness
                    / 100,
                    abs_tol=2,
                ):
                    self.hub.combo_control(
                        True,
                        self.brightness,
                        254,
                        attr_rgb,
                        controller,
                        self.mesh_id,
                        seq,
                    )
                else:
                    self.hub.combo_control(
                        True,
                        round(attr_br * 100 / 255),
                        255,
                        [255, 255, 255],
                        controller,
                        self.mesh_id,
                        seq,
                    )
            elif attr_rgb is None and attr_ct is None and attr_br is not None:
                self.hub.combo_control(
                    True,
                    round(attr_br * 100 / 255),
                    255,
                    [255, 255, 255],
                    controller,
                    self.mesh_id,
                    seq,
                )
            elif attr_rgb is not None and attr_br is None:
                self.hub.combo_control(
                    True,
                    self.brightness,
                    254,
                    attr_rgb,
                    controller,
                    self.mesh_id,
                    seq,
                )
            elif attr_ct is not None:
                ct = round(
                    100
                    * (self.max_mireds - attr_ct)
                    / (self.max_mireds - self.min_mireds)
                )
                self.hub.set_color_temp(ct, controller, self.mesh_id, seq)
            else:
                self.hub.turn_on(controller, self.mesh_id, seq)
            self.hub.pending_commands[seq] = self.command_received
            await asyncio.sleep(self._command_timout)
            if self.hub.pending_commands.get(seq, None) is not None:
                self.hub.pending_commands.pop(seq)
                attempts += 1
            else:
                update_received = True

    async def turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        attempts = 0
        update_received = False
        while not update_received and attempts < int(
            self._command_retry_time / self._command_timout
        ):
            seq = str(self.hub.get_seq_num())
            if len(self.controllers) > 0:
                controller = self.controllers[attempts % len(self.controllers)]
            else:
                controller = self.default_controller
            self.hub.turn_off(controller, self.mesh_id, seq)
            self.hub.pending_commands[seq] = self.command_received
            await asyncio.sleep(self._command_timout)
            if self.hub.pending_commands.get(seq, None) is not None:
                self.hub.pending_commands.pop(seq)
                attempts += 1
            else:
                update_received = True

    def command_received(self, seq):
        """
        Remove command from hub.pending_commands
        when a reply is received from Cync server
        """
        if self.hub.pending_commands.get(seq) is not None:
            self.hub.pending_commands.pop(seq)

    def update_switch(self, state, brightness, color_temp, rgb):
        """
        Update the state of the switch as updates
        are received from the Cync server
        """
        if (
            self.power_state != state
            or self.brightness != brightness
            or self.color_temp != color_temp
            or self.rgb != rgb
        ):
            self.power_state = state
            self.brightness = (
                brightness
                if self.support_brightness and state
                else 100 if state else 0
            )
            self.color_temp = color_temp
            self.rgb = rgb
            self.publish_update()
            if self._update_parent_room:
                self._update_parent_room()

    def update_controllers(self):
        """
        Update the list of responsive,
        Wi-Fi connected controller devices
        """
        connected_devices = self.hub.connected_devices[self.home_id]
        controllers = []
        if len(connected_devices) > 0:
            if int(self.switch_id) > 0:
                if self.device_id in connected_devices:
                    # if this device is connected,
                    # make this the first available controller
                    controllers.append(self.switch_id)
            if self.room:
                controllers = controllers + [
                    self.hub.cync_switches[device_id].switch_id
                    for device_id in self.room.all_room_switches
                    if device_id in connected_devices
                    and device_id != self.device_id
                ]
            others_available = [
                self.hub.cync_switches[device_id].switch_id
                for device_id in connected_devices
            ]
            for controller in controllers:
                if controller in others_available:
                    others_available.remove(controller)
            self.controllers = controllers + others_available
        else:
            self.controllers = [self.default_controller]

    def publish_update(self):
        if self._update_callback:
            self._update_callback()


class CyncMotionSensor:
    def __init__(self, device_id, device_info, room):
        self.device_id = device_id
        self.name = device_info["name"]
        self.home_name = device_info["home_name"]
        self.room = room
        self.motion = False
        self._update_callback = None

    def register(self, update_callback) -> None:
        """Register callback, called when switch changes state."""
        self._update_callback = update_callback

    def reset(self) -> None:
        """Remove previously registered callback."""
        self._update_callback = None

    def update_motion_sensor(self, motion):
        self.motion = motion
        self.publish_update()

    def publish_update(self):
        if self._update_callback:
            self._update_callback()


class CyncAmbientLightSensor:
    def __init__(self, device_id, device_info, room):
        self.device_id = device_id
        self.name = device_info["name"]
        self.home_name = device_info["home_name"]
        self.room = room
        self.ambient_light = False
        self._update_callback = None

    def register(self, update_callback) -> None:
        """Register callback, called when switch changes state."""
        self._update_callback = update_callback

    def reset(self) -> None:
        """Remove previously registered callback."""
        self._update_callback = None

    def update_ambient_light_sensor(self, ambient_light):
        self.ambient_light = ambient_light
        self.publish_update()

    def publish_update(self):
        if self._update_callback:
            self._update_callback()


class CyncUserData:
    def __init__(self):
        self.username = ""
        self.password = ""
        self.auth_code = None
        self.user_credentials = {}

    async def authenticate(self, username, password):
        """Authenticate with the API and get a token."""
        self.username = username
        self.password = password
        auth_data = {
            "corp_id": "1007d2ad150c4000",
            "email": self.username,
            "password": self.password,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(API_AUTH, json=auth_data) as resp:
                if resp.status == 200:
                    self.user_credentials = await resp.json()
                    login_code = (
                        bytearray.fromhex("13000000")
                        + (
                            10 + len(self.user_credentials["authorize"])
                        ).to_bytes(1, "big")
                        + bytearray.fromhex("03")
                        + self.user_credentials["user_id"].to_bytes(4, "big")
                        + len(self.user_credentials["authorize"]).to_bytes(
                            2, "big"
                        )
                        + bytearray(
                            self.user_credentials["authorize"], "ascii"
                        )
                        + bytearray.fromhex("0000b4")
                    )
                    self.auth_code = [
                        int.from_bytes([byt], "big") for byt in login_code
                    ]
                    return {"authorized": True}
                if resp.status == 400:
                    request_code_data = {
                        "corp_id": "1007d2ad150c4000",
                        "email": self.username,
                        "local_lang": "en-us",
                    }
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            API_REQUEST_CODE, json=request_code_data
                        ) as resp:
                            if resp.status == 200:
                                return {
                                    "authorized": False,
                                    "two_factor_code_required": True,
                                }
                            return {
                                "authorized": False,
                                "two_factor_code_required": False,
                            }
                return {
                    "authorized": False,
                    "two_factor_code_required": False,
                }

    async def auth_two_factor(self, code):
        """Authenticate with 2 Factor Code."""
        two_factor_data = {
            "corp_id": "1007d2ad150c4000",
            "email": self.username,
            "password": self.password,
            "two_factor": code,
            "resource": "abcdefghijklmnop",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_2FACTOR_AUTH, json=two_factor_data
            ) as resp:
                if resp.status != 200:
                    return {"authorized": False}

                self.user_credentials = await resp.json()
                login_code = (
                    bytearray.fromhex("13000000")
                    + (10 + len(self.user_credentials["authorize"])).to_bytes(
                        1, "big"
                    )
                    + bytearray.fromhex("03")
                    + self.user_credentials["user_id"].to_bytes(4, "big")
                    + len(self.user_credentials["authorize"]).to_bytes(
                        2, "big"
                    )
                    + bytearray(self.user_credentials["authorize"], "ascii")
                    + bytearray.fromhex("0000b4")
                )
                self.auth_code = [
                    int.from_bytes([byt], "big") for byt in login_code
                ]
                return {"authorized": True}

    async def get_cync_config(self):
        (
            home_devices,
            home_controllers,
            switch_to_home_id_map,
            devices,
            rooms,
        ) = await self._initialize_cync_config()

        if (
            not home_devices
            or not home_controllers
            or not switch_to_home_id_map
        ):
            raise InvalidCyncConfiguration

        return {
            "rooms": rooms,
            "devices": devices,
            "home_devices": home_devices,
            "home_controllers": home_controllers,
            "switchID_to_homeID": switch_to_home_id_map,
        }

    async def _initialize_cync_config(self):
        home_devices = {}
        home_controllers = {}
        switch_to_home_id_map = {}
        devices = {}
        rooms = {}

        homes = await self._get_homes()

        for home in homes:
            home_id, home_info = await self._process_home(home)

            if home_id is None or home_info is None:
                continue

            bulbs_array_length = self._calculate_bulbs_array_length(
                home_id, home_info.get("bulbsArray", [])
            )
            home_devices[home_id] = [""] * bulbs_array_length
            home_controllers[home_id] = []

            home_device_subset, device_subset = self._process_devices(
                home_id, home_info, home_controllers
            )
            home_devices.update(home_device_subset)
            devices.update(device_subset)

            if len(home_controllers[home_id]) > 0:
                self._process_rooms(
                    home_id,
                    home_info,
                    rooms,
                    home_controllers,
                    home_devices,
                    devices,
                )

        return (
            home_devices,
            home_controllers,
            switch_to_home_id_map,
            devices,
            rooms,
        )

    async def _process_home(
        self, home
    ) -> tuple[int, dict[str, Any]] | tuple[None, None]:
        home_id = int(home["id"])
        home_info = await self._get_home_properties(
            home["product_id"], home_id
        )

        if (
            home_info.get("groupsArray", False)
            and home_info.get("bulbsArray", False)
            and len(home_info["groupsArray"]) > 0
            and len(home_info["bulbsArray"]) > 0
        ):
            return home_id, home_info
        return None, None

    def _calculate_bulbs_array_length(
        self, home_id: int, bulbs_array: list[dict[str, Any]]
    ):
        # Calculate the length of bulbs array
        return (
            max(
                (
                    ((int(device["deviceID"]) % home_id) % 1000)
                    + (int((int(device["deviceID"]) % home_id) / 1000) * 256)
                    for device in bulbs_array
                )
            )
            + 1
        )

    def _process_devices(
        self,
        home_id: int,
        home_info: dict[str, Any],
        home_controllers: dict[int, list],
    ):
        home_devices = {}
        devices = {}
        for device in home_info["bulbsArray"]:
            device_id = int(device["deviceID"])
            current_index = ((device_id % home_id) % 1000) + (
                (device_id % home_id) // 1000 * 256
            )
            home_devices[home_id][current_index] = device_id
            devices[device_id] = self._create_device_info(
                device, home_info["name"], current_index
            )

            if "switch_controller" not in devices[device_id]:
                device_type = device["deviceType"]
                if (
                    str(device_type) in CAPABILITIES["MULTIELEMENT"]
                    and current_index < 256
                ):
                    switch_controller = CAPABILITIES["MULTIELEMENT"][
                        str(device_type)
                    ]
                else:
                    switch_controller = devices[
                        home_devices[home_id][home_controllers[home_id][0]]
                    ]["switch_controller"]

                devices[device_id]["switch_controller"] = switch_controller

        return home_devices, devices

    def _create_device_info(
        self, device: dict[str, Any], home_name: str, current_index: int
    ):
        device_type = device["deviceType"]

        return {
            "name": device["displayName"],
            "mesh_id": current_index,
            "switch_id": str(device.get("switchID", 0)),
            "ONOFF": device_type in CAPABILITIES["ONOFF"],
            "BRIGHTNESS": device_type in CAPABILITIES["BRIGHTNESS"],
            "COLORTEMP": device_type in CAPABILITIES["COLORTEMP"],
            "RGB": device_type in CAPABILITIES["RGB"],
            "MOTION": device_type in CAPABILITIES["MOTION"],
            "AMBIENT_LIGHT": device_type in CAPABILITIES["AMBIENT_LIGHT"],
            "WIFICONTROL": device_type in CAPABILITIES["WIFICONTROL"],
            "PLUG": device_type in CAPABILITIES["PLUG"],
            "FAN": device_type in CAPABILITIES["FAN"],
            "home_name": home_name,
            "room": "",
            "room_name": "",
        }

    def _process_rooms(
        self,
        home_id: int,
        home_info,
        rooms,
        home_controllers: dict[int, list],
        home_devices,
        devices,
    ):
        for room in home_info["groupsArray"]:
            if (
                len(room.get("deviceIDArray", []))
                + len(room.get("subgroupIDArray", []))
                > 0
            ):
                room_id, room_controller = self._get_room_info(
                    room, home_id, home_controllers, devices, home_devices
                )
                rooms[room_id] = room_controller
        self._update_subgroups(rooms)

    def _get_room_info(
        self,
        room: dict,
        home_id: int,
        home_controllers,
        devices: dict,
        home_devices: dict,
    ):
        room_id = f'{home_id}- {room["groupID"]}'

        available_room_controllers = [
            (device_id % 1000) + (int(device_id / 1000) * 256)
            for device_id in room.get("deviceIDArray", [])
            if "switch_controller"
            in devices[
                home_devices[home_id][
                    (device_id % 1000) + (int(device_id / 1000) * 256)
                ]
            ]
        ]

        if len(available_room_controllers) > 0:
            room_controller = devices[
                home_devices[home_id][available_room_controllers[0]]
            ]["switch_controller"]
        else:
            room_controller = home_controllers[home_id][0]

        for device_id in room.get("deviceIDArray", []):
            device_id = (device_id % 1000) + (int(device_id / 1000) * 256)
            devices[home_devices[home_id][device_id]]["room"] = room_id
            devices[home_devices[home_id][device_id]]["room_name"] = room[
                "displayName"
            ]
        return room_id, room_controller

    def _update_subgroups(self, rooms):
        for room_info in rooms.values():
            if (
                not room_info.get("isSubgroup", False)
                and len(subgroups := room_info.get("subgroups", [])) > 0
            ):
                for subgroup in subgroups:
                    if rooms.get(subgroup, None):
                        rooms[subgroup]["parent_room"] = room_info["name"]
                    else:
                        room_info["subgroups"].pop(
                            room_info["subgroups"].index(subgroup)
                        )

    async def _get_homes(self):
        """Get a list of devices for a particular user."""
        headers = {"Access-Token": self.user_credentials["access_token"]}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                API_DEVICES.format(user=self.user_credentials["user_id"]),
                headers=headers,
            ) as resp:
                response = await resp.json()
                return response

    async def _get_home_properties(self, product_id, device_id):
        """Get properties for a single device."""
        headers = {"Access-Token": self.user_credentials["access_token"]}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                API_DEVICE_INFO.format(
                    product_id=product_id, device_id=device_id
                ),
                headers=headers,
            ) as resp:
                response = await resp.json()
                return response


class LostConnection(Exception):
    """Lost connection to Cync Server"""


class ShuttingDown(Exception):
    """Cync client shutting down"""


class InvalidCyncConfiguration(Exception):
    """Cync configuration is not supported"""
