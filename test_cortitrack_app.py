import csv
import struct
import threading
import tempfile
import unittest
from pathlib import Path

from cortitrack_app import (
    MeasurementStore,
    decode_ble_payload,
    parse_value,
    read_csv_measurements,
)
from sensor_adapters import DemoSensorAdapter


class CortiTrackTests(unittest.TestCase):
    def test_parse_value_accepts_zero_and_rejects_invalid_values(self):
        self.assertEqual(parse_value("0"), 0)
        for raw in ("nan", "inf", "-1", "not a number"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_value(raw)

    def test_decode_ble_payload_supports_configured_formats(self):
        self.assertEqual(decode_ble_payload(b"12.5", "UTF-8 number"), 12.5)
        little = struct.pack("<f", 4.25)
        big = struct.pack(">f", 4.25)
        self.assertAlmostEqual(
            decode_ble_payload(little, "Float32 little-endian"), 4.25
        )
        self.assertAlmostEqual(decode_ble_payload(big, "Float32 big-endian"), 4.25)
        with self.assertRaises(ValueError):
            decode_ble_payload(b"\x00", "Float32 little-endian")

    def test_demo_adapter_marks_generated_readings_as_simulated(self):
        stop_event = threading.Event()
        readings = []
        statuses = []
        errors = []
        timer = threading.Timer(0.02, stop_event.set)
        timer.start()
        try:
            adapter = DemoSensorAdapter("nmol/L", interval=0.001)
            adapter.run(stop_event, lambda *reading: readings.append(reading), statuses.append, errors.append)
        finally:
            timer.cancel()
        self.assertTrue(readings)
        self.assertEqual(readings[0][2], "SIMULATED (demo)")
        self.assertIn("not a device measurement", readings[0][3])
        self.assertIn("simulated", statuses[0])
        self.assertEqual(errors, [])

    def test_csv_import_and_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "readings.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(("timestamp", "cortisol", "unit", "notes"))
                writer.writerow(
                    ("2026-11-05T08:30:00-05:00", "12.4", "nmol/L", "morning")
                )
            measurements = read_csv_measurements(csv_path)
            self.assertEqual(len(measurements), 1)
            store = MeasurementStore(Path(directory) / "test.sqlite3")
            store.add_many(measurements)
            self.assertEqual(store.recent()[0]["notes"], "morning")
            self.assertEqual(store.all()[0]["value"], 12.4)

    def test_csv_import_rejects_bad_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "bad.csv"
            csv_path.write_text(
                "timestamp,cortisol,unit,notes\n"
                "not-a-date,12.4,nmol/L,bad timestamp\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "line 2"):
                read_csv_measurements(csv_path)


if __name__ == "__main__":
    unittest.main()
