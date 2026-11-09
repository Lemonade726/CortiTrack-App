import asyncio
import csv
import math
import queue
import sqlite3
import struct
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt5.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QLinearGradient,
    QPixmap, QIcon
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QLineEdit,
    QComboBox, QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget,
    QTableWidget, QTableWidgetItem, QFileDialog, QMessageBox, QHeaderView,
    QFormLayout, QSizePolicy, QDoubleSpinBox, QAbstractItemView
)

try:
    from bleak import BleakClient
    BLEAK_AVAILABLE = True
except ImportError:
    BleakClient = None
    BLEAK_AVAILABLE = False


APP_NAME = "CortiTrack"
DATABASE_PATH = Path.home() / ".cortitrack" / "measurements.sqlite3"
ASSETS_PATH = Path(__file__).parent / "Assets"
LOGO_PATH = ASSETS_PATH / "cortitrack_logo_112.png"

UNITS = ("nmol/L", "mcg/dL")
BLE_FORMATS = ("UTF-8 number", "Float32 little-endian", "Float32 big-endian")
SENSOR_SOURCES = ("Demo (simulated)", "Bluetooth LE")
CSV_COLUMNS = ("timestamp", "cortisol", "unit", "notes")

COLORS = {
    "outer": "#33f4d7",
    "cyan": "#0bc8ef",
    "blue": "#179aec",
    "deep_blue": "#2270c6",
    "panel": "#ebfffe",
    "pink": "#e087bc",
    "purple": "#6f56ae",
    "dark_purple": "#0a0648",
    "teal": "#33f4d7",
    "teal_dark": "#1ba68d",
    "dark_blue": "#100c4e",
    "mid_blue": "#011b9e",
    "light_blue": "#0075df",
    "black": "#050505",
    "red": "#fc003b",
    "orange": "#ffb300",
    "white": "#FFFFFF",
    "ink": "#10205E",
}
def parse_value(raw_value):
    """Convert a reading to a finite, non-negative float."""
    value = float(raw_value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Cortisol must be a finite, non-negative number.")
    return value


def parse_timestamp(raw_timestamp):
    """Validate a timestamp and return it in ISO-8601 format."""
    timestamp = datetime.fromisoformat(raw_timestamp.strip().replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    return timestamp.isoformat(timespec="seconds")


def read_csv_measurements(file_path):
    """Read and validate a CSV file using timestamp,cortisol,unit,notes columns."""
    measurements = []
    with open(file_path, "r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise ValueError("The CSV file is empty.")

        missing = set(CSV_COLUMNS) - set(reader.fieldnames)
        if missing:
            raise ValueError(
                "Missing required CSV columns: " + ", ".join(sorted(missing))
            )

        for line_number, row in enumerate(reader, start=2):
            try:
                unit = row["unit"].strip()
                if unit not in UNITS:
                    raise ValueError("unit must be one of: " + ", ".join(UNITS))

                measurements.append({
                    "timestamp": parse_timestamp(row["timestamp"]),
                    "value": parse_value(row["cortisol"]),
                    "unit": unit,
                    "source": "CSV import",
                    "notes": (row.get("notes") or "").strip(),
                })
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid data on CSV line {line_number}: {error}"
                ) from error

    return measurements


# Local database
class MeasurementStore:
    """SQLite storage layer; kept independent from the user interface."""

    def __init__(self, path=DATABASE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL,
                    source TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT ''
                )
            """)

    def add(self, measurement):
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute(
                """INSERT INTO measurements
                   (timestamp, value, unit, source, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    measurement["timestamp"],
                    measurement["value"],
                    measurement["unit"],
                    measurement["source"],
                    measurement.get("notes", ""),
                ),
            )

    def add_many(self, measurements):
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.executemany(
                """INSERT INTO measurements
                   (timestamp, value, unit, source, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        item["timestamp"],
                        item["value"],
                        item["unit"],
                        item["source"],
                        item.get("notes", ""),
                    )
                    for item in measurements
                ],
            )

    def recent(self, limit=500):
        with sqlite3.connect(self.path, timeout=10) as connection:
            rows = connection.execute(
                """SELECT id, timestamp, value, unit, source, notes
                   FROM measurements
                   ORDER BY timestamp DESC, id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

        return [
            {
                "id": row[0],
                "timestamp": row[1],
                "value": row[2],
                "unit": row[3],
                "source": row[4],
                "notes": row[5],
            }
            for row in rows
        ]

    def all(self):
        with sqlite3.connect(self.path, timeout=10) as connection:
            rows = connection.execute(
                """SELECT timestamp, value, unit, source, notes
                   FROM measurements
                   ORDER BY timestamp, id"""
            ).fetchall()

        return [
            {
                "timestamp": row[0],
                "value": row[1],
                "unit": row[2],
                "source": row[3],
                "notes": row[4],
            }
            for row in rows
        ]



# Sensor adapters — included in this file so no sensor_adapters.py is needed

def decode_ble_payload(data, payload_format):
    """Decode a BLE notification payload into a numeric cortisol value."""
    if payload_format == "UTF-8 number":
        raw = bytes(data).decode("utf-8").strip()
        return parse_value(raw)

    if payload_format == "Float32 little-endian":
        if len(data) < 4:
            raise ValueError("BLE payload needs at least 4 bytes for Float32.")
        value = struct.unpack("<f", bytes(data[:4]))[0]
        return parse_value(value)

    if payload_format == "Float32 big-endian":
        if len(data) < 4:
            raise ValueError("BLE payload needs at least 4 bytes for Float32.")
        value = struct.unpack(">f", bytes(data[:4]))[0]
        return parse_value(value)

    raise ValueError(f"Unsupported BLE payload format: {payload_format}")


class DemoSensorAdapter:
    """Creates clearly labeled simulated cortisol values for interface testing."""

    def __init__(self, unit):
        self.unit = unit

    def run(self, stop_event, on_reading, on_status, on_error):
        on_status("Demo mode running — these are simulated test values.")
        # A gentle changing pattern is only for testing the chart and UI.
        value = 8.0
        step = 0
        while not stop_event.wait(3.0):
            value += (0.35 if step % 2 == 0 else -0.18)
            value = max(0.1, value)
            step += 1
            on_reading(value, self.unit, "Demo (simulated)", "Simulated test data")


class BleSensorAdapter:
    """Reads numeric BLE notifications using the selected payload format."""

    def __init__(self, address, characteristic, payload_format, unit):
        self.address = address
        self.characteristic = characteristic
        self.payload_format = payload_format
        self.unit = unit

    def run(self, stop_event, on_reading, on_status, on_error):
        if not BLEAK_AVAILABLE:
            raise RuntimeError(
                "Bluetooth support requires Bleak. Install it with: pip install bleak"
            )
        asyncio.run(self._run_async(stop_event, on_reading, on_status, on_error))

    async def _run_async(self, stop_event, on_reading, on_status, on_error):
        def notification_handler(_sender, data):
            try:
                value = decode_ble_payload(data, self.payload_format)
                on_reading(value, self.unit, "Bluetooth LE", "BLE notification")
            except (ValueError, UnicodeDecodeError, struct.error) as error:
                on_error(f"Could not decode BLE notification: {error}")

        try:
            on_status(f"Connecting to {self.address}…")
            async with BleakClient(self.address) as client:
                if not client.is_connected:
                    raise RuntimeError("Could not connect to the Bluetooth device.")
                await client.start_notify(self.characteristic, notification_handler)
                on_status("Bluetooth connected — waiting for notifications.")
                while not stop_event.is_set() and client.is_connected:
                    await asyncio.sleep(0.2)
                try:
                    await client.stop_notify(self.characteristic)
                except Exception:
                    pass
        except Exception as error:
            on_error(str(error))

# Custom-painted graph and heart-rate waveform
class TrendChart(QWidget):
    """Draws the cortisol trend using the colors and axes from the concept."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.setMinimumHeight(230)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_points(self, points):
        self.points = list(points)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS["panel"]))

        w, h = self.width(), self.height()
        left, right, top, bottom = 48, w - 22, 20, h - 42
        if right <= left or bottom <= top:
            return

        axis_gradient = QLinearGradient(left, top, left, bottom)
        axis_gradient.setColorAt(0, QColor(COLORS["dark_blue"]))
        axis_gradient.setColorAt(0.45, QColor(COLORS["mid_blue"]))
        axis_gradient.setColorAt(1, QColor(COLORS["light_blue"]))
        axis_pen = QPen(QBrush(axis_gradient), 4, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(axis_pen)
        painter.drawLine(left, top + 4, left, bottom)
        painter.drawLine(left, bottom, right, bottom)
        painter.drawLine(left, top + 4, left - 7, top + 14)
        painter.drawLine(left, top + 4, left + 7, top + 14)
        painter.drawLine(right, bottom, right - 11, bottom - 7)
        painter.drawLine(right, bottom, right - 11, bottom + 7)

        if not self.points:
            painter.setPen(QColor(COLORS["teal_dark"]))
            painter.setFont(QFont("Arial Rounded MT Bold", 11))
            painter.drawText(
                self.rect(), Qt.AlignCenter,
                "Your cortisol trend will appear here"
            )
            return

        values = [point["value"] for point in self.points]
        low, high = min(values), max(values)
        spread = high - low or max(abs(high) * 0.1, 1.0)
        low -= spread * 0.12
        high += spread * 0.12

        coordinates = []
        for index, point in enumerate(self.points):
            x = left + (right - left) * index / max(len(self.points) - 1, 1)
            y = bottom - (point["value"] - low) / (high - low) * (bottom - top)
            coordinates.append(QPointF(x, y))

        if len(coordinates) > 1:
            path = QPainterPath(coordinates[0])
            for point in coordinates[1:]:
                path.lineTo(point)
            painter.setPen(QPen(
                QColor(COLORS["teal"]), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin
            ))
            painter.drawPath(path)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLORS["teal_dark"]))
        for point in coordinates:
            painter.drawEllipse(point, 3.2, 3.2)

        painter.setPen(QColor(COLORS["teal_dark"]))
        painter.setFont(QFont("Arial Rounded MT Bold", 9))
        painter.drawText(5, top + 5, f"{high:.3g}")
        painter.drawText(5, bottom, f"{low:.3g}")

        first = datetime.fromisoformat(
            self.points[0]["timestamp"]
        ).astimezone().strftime("%b %d")
        last = datetime.fromisoformat(
            self.points[-1]["timestamp"]
        ).astimezone().strftime("%b %d")
        painter.drawText(left, h - 12, first)
        painter.drawText(right - 55, h - 12, last)


class HeartWave(QWidget):
    """Draws a decorative ECG-style line; this is not measured ECG data."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(85)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        points = [
            QPointF(w * x, h * y)
            for x, y in [
                (0.02, .56), (.12, .56), (.18, .35), (.28, .70),
                (.38, .48), (.48, .48), (.56, .16), (.65, .78),
                (.73, .48), (.98, .48)
            ]
        ]
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)

        gradient = QLinearGradient(0, 0, w, 0)
        gradient.setColorAt(0, QColor(COLORS["red"]))
        gradient.setColorAt(1, QColor("#FFB000"))
        painter.setPen(QPen(
            gradient, 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin
        ))
        painter.drawPath(path)



class GradientBorderFrame(QFrame):
    """Custom angular panel with a pink-to-purple border."""

    def __init__(
        self,
        background_color,
        border_width=5,
        radius=18,
        parent=None,
        background_gradient=None
    ):
        super().__init__(parent)

        self.background_color = QColor(background_color)
        self.background_gradient = background_gradient
        self.border_width = border_width
        self.radius = radius

        self.setFrameShape(QFrame.NoFrame)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def panel_path(self):
        # Use the panel's current size.
        w = self.width()
        h = self.height()

        # Distance used to cut the corners.
        cut = min(35, w / 8, h / 8)

        # Define the angular panel shape.
        path = QPainterPath()
        path.moveTo(cut, 0)
        path.lineTo(w - cut, 0)
        path.lineTo(w, cut)
        path.lineTo(w, h - cut)
        path.lineTo(w - cut, h)
        path.lineTo(cut, h)
        path.lineTo(0, h - cut)
        path.lineTo(0, cut)
        path.closeSubpath()

        return path

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Draw the outer angular shape.
        outer = self.panel_path()

        # Create a slightly smaller shape for the interior.
        inset = self.border_width
        w = self.width()
        h = self.height()
        cut = min(35, w / 8, h / 8)

        inner = QPainterPath()
        inner.moveTo(cut + inset, inset)
        inner.lineTo(w - cut - inset, inset)
        inner.lineTo(w - inset, cut + inset)
        inner.lineTo(w - inset, h - cut - inset)
        inner.lineTo(w - cut - inset, h - inset)
        inner.lineTo(cut + inset, h - inset)
        inner.lineTo(inset, h - cut - inset)
        inner.lineTo(inset, cut + inset)
        inner.closeSubpath()

        # Draw the pink-to-purple border.
        border_gradient = QLinearGradient(0, 0, 0, h)
        border_gradient.setColorAt(0, QColor(COLORS["pink"]))
        border_gradient.setColorAt(1, QColor(COLORS["purple"]))

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(border_gradient))
        painter.drawPath(outer)

        # Draw the panel interior.
        if self.background_gradient:
            fill_gradient = QLinearGradient(0, inset, 0, h - inset)
            fill_gradient.setColorAt(
                0, QColor(self.background_gradient[0])
            )
            fill_gradient.setColorAt(
                1, QColor(self.background_gradient[1])
            )
            painter.setBrush(QBrush(fill_gradient))
        else:
            painter.setBrush(QBrush(self.background_color))

        painter.drawPath(inner)



class HeartRatePanel(GradientBorderFrame):
    """Heart-rate card. Displays unavailable until actual HR data is provided."""

    def __init__(self):
        super().__init__(
            COLORS["black"],
            border_width=5,
            radius=20,
            background_gradient=(
                COLORS["dark_purple"],
                COLORS["black"],
            ),
        )
        self.setObjectName("heartPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 20, 24, 20)
        layout.setSpacing(8)

        self.number = QLabel("—")
        self.number.setObjectName("heartNumber")
        self.number.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.number)

        self.wave = HeartWave()
        layout.addWidget(self.wave, 1)

        self.caption = QLabel("HEART RATE · BPM")
        self.caption.setObjectName("heartCaption")
        self.caption.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.caption)

# Custom tabs

class AngularTabButton(QPushButton):

    def __init__(self, text, parent=None):
        super().__init__(text, parent)

        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(135, 72)
        self.setMouseTracking(True)
        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding
        )

        # Do not use the standard rectangular button appearance.
        self.setStyleSheet(
            "background: transparent; border: none; padding: 0;"
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        # Slanted sides, like the tabs in the concept image.
        cut = 20

        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(w, 0)
        path.lineTo(w - cut, h)
        path.lineTo(cut, h)
        path.closeSubpath()

        # Active tab matches the turquoise outer frame.
        if self.isChecked():
            fill = QColor(COLORS["outer"])
            text_color = QColor(COLORS["black"])
        elif self.underMouse():
            fill = QColor(COLORS["outer"])
            text_color = QColor(COLORS["black"])
        else:
            fill = QColor(COLORS["black"])
            text_color = QColor(COLORS["white"])

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawPath(path)

        # Draw the tab label.
        painter.setPen(text_color)
        font = QFont("Arial Rounded MT Bold", 14)
        font.setBold(True)
        painter.setFont(font)

        painter.drawText(
            self.rect().adjusted(8, 0, -8, 0),
            Qt.AlignCenter,
            self.text()
        )

class AngularNavBar(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(72)
        self.setMinimumWidth(600)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 0, 22, 0)
        layout.setSpacing(0)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        cut = 32

        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(w, 0)
        path.lineTo(w - cut, h)
        path.lineTo(cut, h)
        path.closeSubpath()

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#050505"))
        painter.drawPath(path)


# Main application window
class CortiTrackWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = MeasurementStore()
        self.measurements = []
        self.events = queue.Queue()
        self.sensor_stop = None
        self.sensor_thread = None

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(960, 680)
        self.resize(1400, 900)
        if LOGO_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_PATH)))

        self._build_ui()
        self.refresh()

        self.event_timer = QTimer(self)
        self.event_timer.timeout.connect(self.process_events)
        self.event_timer.start(150)

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        
        # Outer layout with margins and spacing
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0,0,0,0)
        outer.setSpacing(0)

        border = QFrame()
        border.setObjectName("outerFrame")
        outer.addWidget(border, 1)

        root = QVBoxLayout(border)
        root.setContentsMargins(30, 20, 24, 20)
        root.setSpacing(8)

        # Header: logo and navigation
        
        # Header: logo and shared navigation ribbon
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)

        logo = QLabel()
        if LOGO_PATH.exists():
            logo.setPixmap(QPixmap(str(LOGO_PATH)).scaled(
                175, 82, Qt.KeepAspectRatio, Qt.SmoothTransformation
            ))
        else:
            logo.setText("CortiTrack")
            logo.setObjectName("brandFallback")

        logo.setMinimumWidth(175)
        header.addWidget(logo)
        header.addStretch(1)

        # Create the four navigation buttons
        self.nav_dashboard = AngularTabButton("CORTISOL")
        self.nav_readings = AngularTabButton("SKIN")
        self.nav_sensor = AngularTabButton("HEART RATE")
        self.nav_about = AngularTabButton("OVERVIEW")

        self.nav_buttons = [
            self.nav_dashboard,
            self.nav_readings,
            self.nav_sensor,
            self.nav_about,
        ]

        # Put the buttons INSIDE the shared black ribbon
        self.nav_ribbon = AngularNavBar()
        nav_layout = self.nav_ribbon.layout()

        for button in self.nav_buttons:
            nav_layout.addWidget(button, 1)

        header.addWidget(self.nav_ribbon, 3)
        root.addLayout(header)


        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        self.dashboard_page = QWidget()
        self.readings_page = QWidget()
        self.sensor_page = QWidget()
        self.about_page = QWidget()
        for page in (
            self.dashboard_page, self.readings_page,
            self.sensor_page, self.about_page
        ):
            self.pages.addWidget(page)

        self._build_dashboard()
        self._build_readings_page()
        self._build_sensor_page()
        self._build_about_page()

        self.nav_dashboard.clicked.connect(lambda: self.show_page(0))
        self.nav_readings.clicked.connect(lambda: self.show_page(1))
        self.nav_sensor.clicked.connect(lambda: self.show_page(2))
        self.nav_about.clicked.connect(lambda: self.show_page(3))

        self.setStyleSheet(f"""
            QWidget#central {{
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 {COLORS['cyan']},
                    stop:0.45 {COLORS['blue']},
                    stop:1 {COLORS['deep_blue']}
                );
            }}
            QFrame#outerFrame {{
                background: transparent;
                border: 8px solid {COLORS['outer']};
                border-radius: 24px;
            }}
            QLabel#brandFallback {{
                color: {COLORS['white']};
                font-size: 25px;
                font-weight: 900;
            }}
            QPushButton {{
                background: transparent;
                color: {COLORS['white']};
                border: none;
                border-radius: 0;
                padding: 0;
                font-size: 13px;
                font-weight: 900;
            }}

            QPushButton:hover {{
                color: {COLORS['outer']};
            }}
            QFrame#panel {{
                background: transparent;
                border: none;
            }}
            QFrame#heartPanel {{
                background: transparent;
                border: none;
            }}
            QLabel#heartNumber {{
                color: {COLORS['red']};
                font-size: 44px;
                font-weight: 900;
            }}
            QLabel#heartCaption {{
                color: {COLORS['red']};
                font-size: 15px;
                font-weight: 900;
            }}
            QLabel#panelTitle {{
                color: {COLORS['teal_dark']};
                font-size: 18px;
                font-weight: 900;
            }}
            QLabel#infoText {{
                color: {COLORS['teal_dark']};
                font-size: 16px;
                font-weight: 800;
            }}
            QLabel#subtle {{
                color: #E6FFFF;
                font-size: 11px;
            }}
            QLabel {{
                color: #123D55;
            }}
            QLineEdit, QComboBox {{
                background: white;
                color: #14213D;
                border: 2px solid #A4DCD9;
                border-radius: 7px;
                padding: 7px;
                min-height: 22px;
            }}
            QTableWidget {{
                background: {COLORS['panel']};
                alternate-background-color: #D9F7F4;
                color: #14213D;
                gridline-color: #B9DCD9;
                border: 2px solid {COLORS['pink']};
                border-radius: 8px;
                selection-background-color: #B7EEE8;
                selection-color: #14213D;
            }}
            QHeaderView::section {{
                background: #D0F7F2;
                color: #124F55;
                padding: 7px;
                font-weight: bold;
                border: 0;
            }}
        """)

        self.show_page(0)

    def _build_dashboard(self):
        layout = QGridLayout(self.dashboard_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(16)

        chart_panel = GradientBorderFrame(COLORS["panel"])
        chart_panel.setObjectName("panel")
        chart_layout = QVBoxLayout(chart_panel)
        # Leave extra room for the angled panel corners.
        chart_layout.setContentsMargins(38, 20, 30, 22)
        chart_layout.setSpacing(8)

        title_row = QHBoxLayout()
        chart_title = QLabel("CORTISOL LEVELS")
        chart_title.setObjectName("panelTitle")
        title_row.addWidget(chart_title)
        title_row.addStretch(1)

        title_row.addWidget(QLabel("UNIT"))
        self.chart_unit = QComboBox()
        self.chart_unit.addItems(UNITS)
        self.chart_unit.currentTextChanged.connect(self.draw_chart)
        title_row.addWidget(self.chart_unit)
        chart_layout.addLayout(title_row)

        self.chart = TrendChart()
        chart_layout.addWidget(self.chart, 1)
        layout.addWidget(chart_panel, 0, 0, 2, 2)

        self.heart_panel = HeartRatePanel()
        layout.addWidget(self.heart_panel, 0, 2, 1, 1)

        info_panel = GradientBorderFrame(COLORS["panel"])
        info_panel.setObjectName("panel")
        info_layout = QVBoxLayout(info_panel)
        info_layout.setContentsMargins(30, 20, 24, 20)

        info_title = QLabel("YOU ARE —!")
        info_title.setObjectName("panelTitle")
        info_layout.addWidget(info_title)

        self.info_text = QLabel(
            "INFO........\nYour latest reading and tracking status will appear here."
        )
        self.info_text.setObjectName("infoText")
        self.info_text.setWordWrap(True)
        info_layout.addWidget(self.info_text, 1)
        layout.addWidget(info_panel, 1, 2, 1, 1)

        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 1)
        layout.setRowStretch(0, 3)
        layout.setRowStretch(1, 2)

        bottom = QHBoxLayout()
        bottom_label = QLabel("CORTITRACK · PERSONAL TRACKING DASHBOARD")
        bottom_label.setObjectName("subtle")
        bottom.addWidget(bottom_label)
        bottom.addStretch(1)

        quick_add = QPushButton("+ ADD READING")
        quick_add.clicked.connect(lambda: self.show_page(1))
        bottom.addWidget(quick_add)
        layout.addLayout(bottom, 2, 0, 1, 3)

    def _build_readings_page(self):
        layout = QVBoxLayout(self.readings_page)
        layout.setSpacing(12)

        title = QLabel("MEASUREMENTS & MANUAL ENTRY")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        form_frame = GradientBorderFrame(COLORS["panel"])
        form_frame.setObjectName("panel")
        form = QGridLayout(form_frame)
        form.setContentsMargins(16, 16, 16, 16)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        form.addWidget(QLabel("Cortisol value"), 0, 0)
        self.value_input = QLineEdit()
        self.value_input.setPlaceholderText("Enter a non-negative number")
        form.addWidget(self.value_input, 1, 0)

        form.addWidget(QLabel("Unit"), 0, 1)
        self.unit_input = QComboBox()
        self.unit_input.addItems(UNITS)
        form.addWidget(self.unit_input, 1, 1)

        form.addWidget(QLabel("Notes (optional)"), 0, 2)
        self.notes_input = QLineEdit()
        self.notes_input.setPlaceholderText("Context, activity, or notes")
        form.addWidget(self.notes_input, 1, 2)

        save = QPushButton("SAVE READING")
        save.clicked.connect(self.save_manual)
        form.addWidget(save, 1, 3)

        for column in range(4):
            form.setColumnStretch(column, 1)
        layout.addWidget(form_frame)

        toolbar = QHBoxLayout()
        import_button = QPushButton("IMPORT CSV")
        import_button.clicked.connect(self.import_csv)
        export_button = QPushButton("EXPORT CSV")
        export_button.clicked.connect(self.export_csv)
        toolbar.addWidget(import_button)
        toolbar.addWidget(export_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Date & time", "Cortisol", "Unit", "Source", "Notes"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        layout.addWidget(self.table, 1)

    def _build_sensor_page(self):
        layout = QVBoxLayout(self.sensor_page)
        title = QLabel("SENSOR CONNECTION")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        panel = GradientBorderFrame(COLORS["panel"])
        panel.setObjectName("panel")
        form = QFormLayout(panel)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(12)

        self.sensor_source = QComboBox()
        self.sensor_source.addItems(SENSOR_SOURCES)
        self.sensor_source.currentTextChanged.connect(self.update_sensor_fields)

        self.device_input = QLineEdit()
        self.device_input.setPlaceholderText("Bluetooth device address / ID")
        self.characteristic_input = QLineEdit()
        self.characteristic_input.setPlaceholderText(
            "Notification characteristic UUID"
        )
        self.payload_format = QComboBox()
        self.payload_format.addItems(BLE_FORMATS)

        form.addRow("Sensor source", self.sensor_source)
        form.addRow("Device address / ID", self.device_input)
        form.addRow("Notify characteristic UUID", self.characteristic_input)
        form.addRow("Payload format", self.payload_format)

        self.sensor_status = QLabel(
            "Demo mode creates simulated test readings. Select Bluetooth LE "
            "only when you know your device's address, characteristic UUID, "
            "and notification payload format."
        )
        self.sensor_status.setWordWrap(True)
        form.addRow("Status", self.sensor_status)

        self.sensor_button = QPushButton("START TRACKING")
        self.sensor_button.clicked.connect(self.toggle_sensor)
        form.addRow(self.sensor_button)

        if not BLEAK_AVAILABLE:
            self.sensor_status.setText(
                "Demo mode is available. To use Bluetooth LE, install Bleak "
                "with: pip install bleak"
            )

        layout.addWidget(panel)
        layout.addStretch(1)
        self.update_sensor_fields()

    def _build_about_page(self):
        layout = QVBoxLayout(self.about_page)
        panel = GradientBorderFrame(COLORS["panel"])
        panel.setObjectName("panel")
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(24, 24, 24, 24)

        title = QLabel("ABOUT CORTITRACK")
        title.setObjectName("panelTitle")
        inner.addWidget(title)

        body = QLabel(
            "A private, on-device journal for cortisol readings.\n\n"
            "Readings are stored locally on this computer. This prototype "
            "is for personal tracking only; readings are not a diagnosis. "
            "Follow your device instructions and discuss results with a clinician.\n\n"
            "Demo readings are simulated test data. The heart-rate card is "
            "a visual placeholder until a source that actually supplies "
            "heart-rate data is connected."
        )
        body.setObjectName("infoText")
        body.setWordWrap(True)
        inner.addWidget(body)

        layout.addWidget(panel)
        layout.addStretch(1)


    # Navigation and input handling
    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)
            button.update()

    def update_sensor_fields(self, *_):
        is_bluetooth = self.sensor_source.currentText() == "Bluetooth LE"
        self.device_input.setEnabled(is_bluetooth)
        self.characteristic_input.setEnabled(is_bluetooth)
        self.payload_format.setEnabled(is_bluetooth)

    def save_manual(self):
        try:
            value = parse_value(self.value_input.text())
            measurement = {
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "value": value,
                "unit": self.unit_input.currentText(),
                "source": "Manual",
                "notes": self.notes_input.text().strip(),
            }
            self.store.add(measurement)
        except (ValueError, sqlite3.Error, OSError) as error:
            QMessageBox.critical(self, "Could not save reading", str(error))
            return

        self.value_input.clear()
        self.notes_input.clear()
        self.refresh()
        self.show_page(0)
        QMessageBox.information(self, "Saved", "Your reading was saved locally.")

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import measurements", "", "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return

        try:
            measurements = read_csv_measurements(path)
            self.store.add_many(measurements)
        except (OSError, ValueError, sqlite3.Error) as error:
            QMessageBox.critical(self, "Could not import CSV", str(error))
            return

        self.refresh()
        QMessageBox.information(
            self, "Import complete",
            f"Imported {len(measurements)} measurement(s)."
        )

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export measurements", "cortitrack_measurements.csv",
            "CSV files (*.csv)"
        )
        if not path:
            return

        try:
            measurements = self.store.all()
            with open(path, "w", encoding="utf-8", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                for measurement in measurements:
                    writer.writerow({
                        "timestamp": measurement["timestamp"],
                        "cortisol": measurement["value"],
                        "unit": measurement["unit"],
                        "notes": measurement["notes"],
                    })
        except (OSError, sqlite3.Error) as error:
            QMessageBox.critical(self, "Could not export CSV", str(error))
            return

        QMessageBox.information(
            self, "Export complete",
            f"Exported {len(measurements)} measurement(s)."
        )

    # Sensor lifecycle. Sensor callbacks run off the UI thread and send
    # events through a queue; process_events updates Qt widgets safely.
    def toggle_sensor(self):
        if self.sensor_thread and self.sensor_thread.is_alive():
            if self.sensor_stop:
                self.sensor_stop.set()
            self.sensor_status.setText("Stopping sensor…")
            return

        unit = self.unit_input.currentText()
        if self.sensor_source.currentText() == SENSOR_SOURCES[0]:
            adapter = DemoSensorAdapter(unit)
        else:
            if not BLEAK_AVAILABLE:
                QMessageBox.warning(
                    self, "Bluetooth support missing",
                    "Install Bluetooth support with: pip install bleak"
                )
                return

            address = self.device_input.text().strip()
            characteristic = self.characteristic_input.text().strip()
            if not address:
                QMessageBox.critical(
                    self, "Bluetooth setup", "Enter the device address or ID."
                )
                return
            try:
                characteristic = str(uuid.UUID(characteristic))
            except ValueError:
                QMessageBox.critical(
                    self, "Bluetooth setup",
                    "Enter a valid notification characteristic UUID."
                )
                return

            adapter = BleSensorAdapter(
                address, characteristic, self.payload_format.currentText(), unit
            )

        self.sensor_stop = threading.Event()
        self.sensor_button.setText("STOP TRACKING")
        self.sensor_status.setText("Starting sensor…")
        self.sensor_thread = threading.Thread(
            target=self.run_sensor,
            args=(adapter, self.sensor_stop),
            daemon=True,
        )
        self.sensor_thread.start()

    def run_sensor(self, adapter, stop_event):
        try:
            adapter.run(
                stop_event,
                self.on_sensor_reading,
                lambda detail: self.events.put(("status", detail)),
                lambda detail: self.events.put(("sensor_error", detail)),
            )
        except Exception as error:
            self.events.put(("error", str(error)))
        finally:
            self.events.put(("disconnected", "Sensor disconnected."))

    def on_sensor_reading(self, value, unit, source, notes):
        try:
            measurement = {
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "value": parse_value(value),
                "unit": unit,
                "source": source,
                "notes": notes,
            }
            self.store.add(measurement)
            self.events.put((
                "reading",
                f"Saved {source} reading: {measurement['value']:g} {unit}"
            ))
        except (ValueError, sqlite3.Error, OSError) as error:
            self.events.put(("sensor_error", f"Could not save sensor reading: {error}"))

    def process_events(self):
        try:
            while True:
                event, detail = self.events.get_nowait()
                if event in ("status", "reading"):
                    self.sensor_status.setText(detail)
                    if event == "reading":
                        self.refresh()
                elif event in ("error", "sensor_error"):
                    self.sensor_status.setText(detail)
                    QMessageBox.critical(self, "Sensor error", detail)
                elif event == "disconnected":
                    self.sensor_status.setText(detail)
                    self.sensor_button.setText("START TRACKING")
                    self.refresh()
        except queue.Empty:
            pass

    
    # Refresh graph, table, and latest-reading panel
    def refresh(self):
        self.measurements = self.store.recent()

        if hasattr(self, "table"):
            self.table.setRowCount(len(self.measurements))
            for row, measurement in enumerate(self.measurements):
                timestamp = datetime.fromisoformat(
                    measurement["timestamp"]
                ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                values = [
                    timestamp,
                    f"{measurement['value']:g}",
                    measurement["unit"],
                    measurement["source"],
                    measurement["notes"],
                ]
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QTableWidgetItem(value))

        self.draw_chart()

        if self.measurements:
            latest = self.measurements[0]
            timestamp = datetime.fromisoformat(
                latest["timestamp"]
            ).astimezone().strftime("%b %d, %Y at %H:%M")
            notes = latest["notes"] or "No notes recorded."
            self.info_text.setText(
                f"LATEST READING\n{latest['value']:g} {latest['unit']}\n"
                f"{timestamp}\nSource: {latest['source']}\n{notes}"
            )
        else:
            self.info_text.setText(
                "INFO........\nNo readings saved yet. Add a reading or "
                "connect a sensor to get started."
            )

    def draw_chart(self, *_):
        if not hasattr(self, "chart"):
            return
        unit = self.chart_unit.currentText()
        points = [
            measurement
            for measurement in reversed(self.measurements)
            if measurement["unit"] == unit
        ][-30:]
        self.chart.set_points(points)

    def closeEvent(self, event):
        if self.sensor_stop:
            self.sensor_stop.set()
        event.accept()


def main():
    app = QApplication([])
    app.setApplicationName(APP_NAME)
    window = CortiTrackWindow()
    window.showMaximized()
    app.exec_()


if __name__ == "__main__":
    main()
