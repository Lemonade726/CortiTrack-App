# CortiTrack

A local-first desktop journal for cortisol readings. Measurements stay in a
SQLite database on the current computer; the app does not upload them.

## Run

Requires Python 3.10 or newer. Install the dependencies and choose a desktop UI:

```powershell
python -m pip install -r requirements.txt
```

Run the Tkinter version:

```powershell
python cortitrack_app.py
```

Run the full PyQt5 dashboard:

```powershell
python CortiTrack_PyQt5_Full.py
```

Manual entry and CSV import/export work without a sensor. The Continuous sensor
panel offers a demo stream for testing the interface and a Bluetooth LE adapter
for compatible hardware; Bluetooth LE support uses the `bleak` package.

## Sensor adapters

Choose **Demo (simulated)** to preview continuous updates. Demo values are
artificial, are marked `SIMULATED (demo)` in the history, and must not be treated
as measurements.

The Bluetooth LE adapter is an initial integration example. For a compatible
device, choose **Bluetooth LE**, enter the device address/ID and notification
characteristic UUID, then choose the payload format:

- **UTF-8 number**: a notification containing a number such as `12.4`
- **Float32 little-endian**: a four-byte IEEE 754 float, little-endian
- **Float32 big-endian**: a four-byte IEEE 754 float, big-endian

Select the unit reported by the device before connecting. CortiTrack listens for
notifications; it does not configure the device, perform calibration, or infer
units. Check your device documentation for its BLE address, characteristic, and
payload format. Bluetooth availability and permissions depend on the operating
system and adapter.

The Tkinter version's sensor integrations live in `sensor_adapters.py`. A future
hardware adapter can implement `SensorAdapter.run(...)` and send validated
readings through the same callback contract, without changing the history, chart,
or storage code. Actual sensor integration will still require the device
communication protocol, measurement units, calibration details, and validation
information.

## CSV format

Import files must have these headers: `timestamp,cortisol,unit,notes`. Timestamps
should be ISO 8601 (for example `2026-11-05T08:30:00-05:00`). Supported units are
`nmol/L`, `µg/dL`, and `µg/L`. Invalid rows reject the import rather than being
silently skipped. Exports use the same columns.

The local database is stored at `~/.cortitrack/measurements.sqlite3`. Back it up
or export a CSV to retain a copy.

## Important

CortiTrack is a personal tracking prototype, not a medical device or diagnostic
service. Demo readings are fictitious. Hormone readings can depend on sample
type, collection time, and assay. Use the device manufacturer's instructions
and discuss results with a qualified healthcare professional.
