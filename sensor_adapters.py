import asyncio
import random
import struct
import threading
from abc import ABC, abstractmethod


def decode_ble_payload(payload, data_format):
    if data_format == "UTF-8 number":
        raw_value = payload.decode("utf-8").strip()
    elif data_format == "Float32 little-endian":
        if len(payload) != 4:
            raise ValueError("Expected exactly 4 bytes for a Float32 reading.")
        raw_value = struct.unpack("<f", payload)[0]
    elif data_format == "Float32 big-endian":
        if len(payload) != 4:
            raise ValueError("Expected exactly 4 bytes for a Float32 reading.")
        raw_value = struct.unpack(">f", payload)[0]
    else:
        raise ValueError(f"Unsupported BLE data format: {data_format}")
    value = float(raw_value)
    if value < 0 or value == float("inf") or value != value:
        raise ValueError("Cortisol must be a finite, non-negative number.")
    return value


class SensorAdapter(ABC):
    source_name = "Sensor"

    @abstractmethod
    def run(self, stop_event, on_reading, on_status, on_error):
        raise NotImplementedError


class DemoSensorAdapter(SensorAdapter):
    source_name = "SIMULATED (demo)"

    def __init__(self, unit, interval=3):
        self.unit = unit
        self.interval = interval

    def run(self, stop_event, on_reading, on_status, on_error):
        on_status("Demo connected; values below are simulated, not sensor readings.")
        while not stop_event.wait(self.interval):
            on_reading(
                random.uniform(5, 25),
                self.unit,
                self.source_name,
                "Generated demo data; not a device measurement.",
            )


class BleSensorAdapter(SensorAdapter):
    source_name = "Bluetooth LE"

    def __init__(self, address, characteristic, data_format, unit):
        self.address = address
        self.characteristic = characteristic
        self.data_format = data_format
        self.unit = unit

    def run(self, stop_event, on_reading, on_status, on_error):
        try:
            asyncio.run(
                self._run_async(stop_event, on_reading, on_status, on_error)
            )
        except ImportError as error:
            raise RuntimeError(
                "Bluetooth support requires Bleak. Install it with: "
                "python -m pip install -r requirements.txt"
            ) from error

    async def _run_async(self, stop_event, on_reading, on_status, on_error):
        from bleak import BleakClient

        def handle_notification(_sender, payload):
            try:
                value = decode_ble_payload(bytes(payload), self.data_format)
                on_reading(value, self.unit, self.source_name, "")
            except (UnicodeDecodeError, ValueError) as error:
                on_error(f"Could not decode Bluetooth reading: {error}")

        async with BleakClient(self.address) as client:
            await client.start_notify(self.characteristic, handle_notification)
            on_status("Connected; waiting for readings...")
            while not stop_event.is_set():
                await asyncio.sleep(0.2)
            await client.stop_notify(self.characteristic)
