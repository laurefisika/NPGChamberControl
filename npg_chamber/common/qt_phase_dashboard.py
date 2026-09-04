"""Professional PySide6 + PyQtGraph dashboard for Phase 01 and Phase 03.

The phase scripts remain the owners of hardware communication, PID/rate control,
safety watchdogs, phase transitions and data persistence.  This module provides
an explicitly separated presentation runtime:

* a dedicated telemetry thread reads bounded, immutable snapshots;
* a latest-value mailbox prevents GUI event backlogs;
* a serialized priority command queue executes operator actions away from Qt;
* the Qt main thread only validates input, renders widgets and paints curves.

No serial I/O, PID calculation, hardware lock or full-run file write is performed
by the Qt event loop.
"""

from __future__ import annotations

import itertools
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping


Snapshot = Mapping[str, Mapping[str, list[Any]]]
Status = Mapping[str, Any]


def relative_thickness_series(
    times: list[Any],
    values: list[Any],
    *,
    baseline: float | None,
    start_timestamp: float | None,
) -> tuple[list[Any], list[float]]:
    """Return the post-shutter thickness trace referenced to 0 Å.

    The phase scripts keep the instrument/raw history untouched (Phase 01) or
    may reset their own evaporation window (Phase 03).  The dashboard therefore
    performs this small presentation transform from the explicit baseline and
    shutter timestamp supplied by the phase.  A synthetic 0 Å point at the
    confirmation instant makes the physical reference unambiguous on screen.
    """

    if baseline is None or start_timestamp is None:
        return list(times), list(values)
    try:
        base = float(baseline)
        start = float(start_timestamp)
    except Exception:
        return list(times), list(values)
    if not (math.isfinite(base) and math.isfinite(start)):
        return list(times), list(values)

    out_times: list[Any] = [start]
    out_values: list[float] = [0.0]
    for stamp, raw in zip(list(times), list(values)):
        try:
            ts = stamp.timestamp() if hasattr(stamp, "timestamp") else float(stamp)
            value = float(raw)
        except Exception:
            continue
        if not (math.isfinite(ts) and math.isfinite(value)) or ts < start:
            continue
        out_times.append(stamp)
        out_values.append(value - base)
    return out_times, out_values


def parse_locale_flexible_float(text: str, label: str = "Value") -> float:
    """Parse one operator-entered decimal without locale-dependent scaling.

    Phase 01/03 must accept both ``0.2`` and ``0,2`` on Windows machines
    configured with either decimal convention. Thousands/group separators are
    deliberately unsupported because they are unsafe and unnecessary for chamber
    control values.
    """
    raw = str(text).strip()
    if not raw:
        raise ValueError(f"{label} is empty")
    if "." in raw and "," in raw:
        raise ValueError(
            f"{label} must use a single decimal separator ('.' or ','): {raw!r}"
        )
    normalized = raw.replace(",", ".")
    try:
        value = float(normalized)
    except Exception as exc:
        raise ValueError(f"{label} is not a valid number: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


@dataclass(frozen=True)
class PhaseDashboardSpec:
    """Inputs required by the shared Phase 01/03 Qt dashboard."""

    window_title: str
    phase_name: str
    snapshot_provider: Callable[[], Snapshot]
    status_provider: Callable[[], Status]
    stop_event: threading.Event
    apply_targets: Callable[[float, float, float], None]
    reset_targets: Callable[[], None]
    apply_ramp: Callable[[str, float, float, float, float, float], None]
    reset_ramp: Callable[[], None]
    set_manual_current: Callable[[float], None]
    resume_automatic_current: Callable[[], None]
    open_shutter: Callable[[], None]
    close_shutter: Callable[[], None]
    abort: Callable[[], None]
    finish: Callable[[], None]
    set_temperature_view: Callable[[str], None]
    set_feedback_mode: Callable[[str], None] | None = None
    refresh_interval_ms: int = 250
    telemetry_interval_ms: int = 200
    telemetry_stale_after_s: float = 2.0
    max_plot_points: int = 400
    command_queue_size: int = 32


@dataclass(frozen=True)
class TelemetryFrame:
    """One complete GUI frame produced outside the Qt event loop."""

    sequence: int
    captured_monotonic: float
    poll_duration_s: float
    snapshot: Snapshot
    status: Status


@dataclass(frozen=True)
class TelemetryCaptureResult:
    """Result of one fault-isolated telemetry acquisition cycle.

    Snapshot and status providers are deliberately isolated. A presentation-only
    status error must never blank otherwise valid live curves, and a temporary
    snapshot error must not erase the last useful process status.
    """

    frame: TelemetryFrame
    errors: tuple[str, ...]


def capture_telemetry_frame(
    spec: PhaseDashboardSpec,
    sequence: int,
    *,
    fallback_snapshot: Snapshot | None = None,
    fallback_status: Status | None = None,
) -> TelemetryCaptureResult:
    """Capture one GUI frame while isolating snapshot and status failures."""

    started = time.monotonic()
    errors: list[str] = []

    try:
        snapshot = spec.snapshot_provider()
    except Exception as exc:
        snapshot = fallback_snapshot or {}
        errors.append(f"Snapshot provider failed: {exc}")

    try:
        status = spec.status_provider()
    except Exception as exc:
        status = dict(fallback_status or {})
        status.setdefault("phase", "TELEMETRY DEGRADED")
        status.setdefault("phase_label", "TELEMETRY DEGRADED")
        status.setdefault("status_lines", ["Live status is temporarily unavailable."])
        errors.append(f"Status provider failed: {exc}")

    if errors:
        mutable_status = dict(status)
        mutable_status["telemetry_errors"] = tuple(errors)
        status = mutable_status

    finished = time.monotonic()
    return TelemetryCaptureResult(
        frame=TelemetryFrame(
            sequence=int(sequence),
            captured_monotonic=finished,
            poll_duration_s=finished - started,
            snapshot=snapshot,
            status=status,
        ),
        errors=tuple(errors),
    )


class LatestFrameMailbox:
    """Thread-safe single-frame mailbox with latest-value semantics.

    The telemetry producer may publish faster than the display can paint.  Only
    the newest complete frame is retained, so the Qt event queue can never fill
    with obsolete plotting work during a long experimental run.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: TelemetryFrame | None = None

    def publish(self, frame: TelemetryFrame) -> None:
        with self._lock:
            self._latest = frame

    def latest_after(self, sequence: int) -> TelemetryFrame | None:
        with self._lock:
            frame = self._latest
        if frame is None or frame.sequence <= sequence:
            return None
        return frame


def qt_dashboard_available() -> bool:
    """Return whether both runtime GUI dependencies can be imported."""

    try:
        import PySide6  # noqa: F401
        import pyqtgraph  # noqa: F401
    except Exception:
        return False
    return True


def run_phase_dashboard(spec: PhaseDashboardSpec) -> int:
    """Run the shared professional Qt dashboard until the phase stops."""

    if not qt_dashboard_available():
        raise RuntimeError(
            "PySide6 and PyQtGraph are required for the fast Phase 01/03 GUI. "
            "Re-run START_NPG_CHAMBER.bat so the project dependencies are installed."
        )

    # Qt scale-related environment variables must be set before QApplication.
    if os.environ.get("NPG_QT_SCALE_FACTOR"):
        os.environ.setdefault("QT_SCALE_FACTOR", os.environ["NPG_QT_SCALE_FACTOR"])

    # Local imports keep diagnostics and non-GUI tests importable on systems
    # where the optional Qt runtime is intentionally absent.
    from PySide6 import QtCore, QtGui, QtWidgets
    import pyqtgraph as pg

    pg.setConfigOptions(antialias=False, background="#ffffff", foreground="#334155")

    class TelemetryThread(QtCore.QThread):
        """Acquire presentation snapshots without ever blocking the GUI thread."""

        failed = QtCore.Signal(str)

        def __init__(self, mailbox: LatestFrameMailbox, parent=None) -> None:
            super().__init__(parent)
            self._mailbox = mailbox
            self._shutdown = threading.Event()
            self._sequence = 0
            self._last_error_text = ""
            self._last_error_reported_at = 0.0
            self._last_snapshot: Snapshot = {}
            self._last_status: Status = {}

        def request_shutdown(self) -> None:
            self._shutdown.set()

        def run(self) -> None:
            interval_s = max(0.05, float(spec.telemetry_interval_ms) / 1000.0)
            while not self._shutdown.is_set() and not spec.stop_event.is_set():
                cycle_started = time.monotonic()
                self._sequence += 1
                result = capture_telemetry_frame(
                    spec,
                    self._sequence,
                    fallback_snapshot=self._last_snapshot,
                    fallback_status=self._last_status,
                )
                self._mailbox.publish(result.frame)
                self._last_snapshot = result.frame.snapshot
                self._last_status = result.frame.status

                if result.errors:
                    now = time.monotonic()
                    error_text = " | ".join(result.errors)
                    if (
                        error_text != self._last_error_text
                        or now - self._last_error_reported_at >= 5.0
                    ):
                        self.failed.emit(error_text)
                        self._last_error_text = error_text
                        self._last_error_reported_at = now
                else:
                    self._last_error_text = ""

                elapsed = time.monotonic() - cycle_started
                self._shutdown.wait(max(0.0, interval_s - elapsed))

    class CommandThread(QtCore.QThread):
        """Execute operator commands sequentially with abort priority."""

        started = QtCore.Signal(str)
        completed = QtCore.Signal(str)
        failed = QtCore.Signal(str, str)

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._queue: queue.PriorityQueue[tuple[int, int, Any]] = queue.PriorityQueue(
                maxsize=max(4, int(spec.command_queue_size))
            )
            self._sequence = itertools.count()
            self._accepting = True
            self._state_lock = threading.Lock()

        def submit(
            self,
            label: str,
            callback: Callable[..., None],
            args: tuple[Any, ...],
            *,
            priority: int = 10,
        ) -> bool:
            request = (str(label), callback, tuple(args))
            with self._state_lock:
                if not self._accepting:
                    return False
                try:
                    self._queue.put_nowait(
                        (int(priority), next(self._sequence), request)
                    )
                except queue.Full:
                    return False
            return True

        def pending_count(self) -> int:
            return self._queue.qsize()

        def request_shutdown(self, *, cancel_pending: bool = True) -> None:
            with self._state_lock:
                if not self._accepting:
                    return
                self._accepting = False

            if cancel_pending:
                # Once the phase stop event is set, queued UI requests are stale
                # and must not execute after the established cleanup path.
                while True:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self._queue.task_done()

            # Non-blocking insertion is guaranteed after the optional drain.
            self._queue.put_nowait((10_000, next(self._sequence), None))

        def run(self) -> None:
            while True:
                _priority, _sequence, request = self._queue.get()
                try:
                    if request is None:
                        return
                    label, callback, args = request
                    self.started.emit(label)
                    try:
                        callback(*args)
                    except Exception as exc:
                        self.failed.emit(label, str(exc))
                    else:
                        self.completed.emit(label)
                finally:
                    self._queue.task_done()

    class TimeAxis(pg.DateAxisItem):
        """Date axis with compact clock labels for live chamber telemetry."""

        def tickStrings(self, values, scale, spacing):  # noqa: N802 - Qt API name
            labels: list[str] = []
            for value in values:
                try:
                    labels.append(datetime.fromtimestamp(float(value)).strftime("%H:%M:%S"))
                except Exception:
                    labels.append("")
            return labels

    class RawValueAxis(pg.AxisItem):
        """Y axis that prints the actual measured value without a global multiplier.

        PyQtGraph's automatic SI-prefix scaling is useful for generic plots but is
        awkward beside laboratory instruments: an operator should read ``0.005`` A
        or ``0.020`` Å/s directly rather than mentally applying an ``x10^-3`` factor.
        Logarithmic axes keep PyQtGraph's native decade formatting.
        """

        @staticmethod
        def _format_raw_value(value: float) -> str:
            value = float(value)
            if not math.isfinite(value):
                return ""
            magnitude = abs(value)
            if magnitude == 0.0:
                return "0"
            if magnitude >= 10000.0:
                return f"{value:.0f}"
            if magnitude >= 1000.0:
                return f"{value:.1f}".rstrip("0").rstrip(".")
            if magnitude >= 100.0:
                return f"{value:.1f}".rstrip("0").rstrip(".")
            if magnitude >= 10.0:
                return f"{value:.2f}".rstrip("0").rstrip(".")
            if magnitude >= 1.0:
                return f"{value:.3f}".rstrip("0").rstrip(".")
            if magnitude >= 0.01:
                return f"{value:.3f}".rstrip("0").rstrip(".")
            if magnitude >= 0.000001:
                return f"{value:.6f}".rstrip("0").rstrip(".")
            return f"{value:.2e}"

        def tickStrings(self, values, scale, spacing):  # noqa: N802 - Qt API name
            if getattr(self, "logMode", False):
                return super().tickStrings(values, 1.0, spacing)
            return [self._format_raw_value(value) for value in values]

    class CollapsibleSection(QtWidgets.QWidget):
        """Lightweight Phase-02-style expandable card.

        The body is hidden without animation so collapsing a section immediately
        removes it from layout and painting work.  This mirrors the HTML
        ``<details>`` cards used by Phase 02 while remaining native Qt.
        """

        def __init__(self, title: str, body: QtWidgets.QWidget, *, expanded: bool = False):
            super().__init__()
            self.setObjectName("collapsibleSection")
            self._body = body

            section_layout = QtWidgets.QVBoxLayout(self)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(0)

            self.toggle = QtWidgets.QToolButton()
            self.toggle.setObjectName("sectionToggle")
            self.toggle.setText(str(title))
            self.toggle.setCheckable(True)
            self.toggle.setChecked(bool(expanded))
            self.toggle.setToolButtonStyle(
                QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            self.toggle.setArrowType(
                QtCore.Qt.ArrowType.DownArrow
                if expanded
                else QtCore.Qt.ArrowType.RightArrow
            )
            self.toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.toggle.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            self.toggle.toggled.connect(self._set_expanded)
            section_layout.addWidget(self.toggle)

            body_frame = QtWidgets.QFrame()
            body_frame.setObjectName("sectionBody")
            body_layout = QtWidgets.QVBoxLayout(body_frame)
            body_layout.setContentsMargins(8, 8, 8, 8)
            body_layout.addWidget(body)
            body_frame.setVisible(bool(expanded))
            self._body_frame = body_frame
            section_layout.addWidget(body_frame)

        def _set_expanded(self, expanded: bool) -> None:
            self.toggle.setArrowType(
                QtCore.Qt.ArrowType.DownArrow
                if expanded
                else QtCore.Qt.ArrowType.RightArrow
            )
            self._body_frame.setVisible(bool(expanded))

        def is_expanded(self) -> bool:
            return self.toggle.isChecked()


    class DashboardWindow(QtWidgets.QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(spec.window_title)
            self.resize(1900, 1050)
            self.setMinimumSize(1280, 760)

            self._closing_from_stop = False
            self._abort_dispatched = False
            self._runtime_shutdown = False
            self._curves: dict[str, Any] = {}
            self._curve_has_data: dict[str, bool] = {}
            self._plots: dict[str, Any] = {}
            self._reference_lines: dict[str, Any] = {}
            self._shutter_reference_lines: dict[str, Any] = {}
            self._shutter_close_reference_lines: dict[str, Any] = {}
            self._last_phase = ""
            self._last_status: Status = {}
            self._last_frame_sequence = 0
            self._last_frame_monotonic = 0.0
            self._last_poll_duration_s = 0.0
            self._last_render_duration_s = 0.0
            self._dropped_frames = 0
            self._pending_command_labels: set[str] = set()
            self._command_state = "idle"
            self._mailbox = LatestFrameMailbox()
            self._feedback_mode_dirty = False
            self._target_edit_dirty = {"temp": False, "rate": False, "band": False}
            self._pending_target_values: dict[str, float] | None = None
            self._last_status_text = ""
            self._last_action_text = ""
            self._last_runtime_health_text = ""
            self._last_temperature_view = ""
            self._shutter_open_timestamp: float | None = None
            self._shutter_close_timestamp: float | None = None
            self._relative_thickness_active = False
            self._relative_thickness_baselines: dict[str, float | None] = {
                "ck1": None, "sample": None
            }
            self._autoscale_ranges: dict[str, tuple[float, float]] = {}

            self._build_ui()
            self._apply_window_style()
            self._start_runtime_threads()

            self._render_timer = QtCore.QTimer(self)
            self._render_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
            self._render_timer.timeout.connect(self._refresh_from_mailbox)
            self._render_timer.start(max(100, int(spec.refresh_interval_ms)))

            self._health_timer = QtCore.QTimer(self)
            self._health_timer.setTimerType(QtCore.Qt.TimerType.CoarseTimer)
            self._health_timer.timeout.connect(self._runtime_health_tick)
            self._health_timer.start(250)
            self._runtime_health_tick()

        def _start_runtime_threads(self) -> None:
            self._telemetry_thread = TelemetryThread(self._mailbox, self)
            self._telemetry_thread.failed.connect(self._show_telemetry_error)
            self._telemetry_thread.start()

            self._command_thread = CommandThread(self)
            self._command_thread.started.connect(self._command_started)
            self._command_thread.completed.connect(self._command_completed)
            self._command_thread.failed.connect(self._command_failed)
            self._command_thread.start()

        # ---------- UI construction ----------
        def _apply_window_style(self) -> None:
            self.setStyleSheet(
                """
                QMainWindow, QWidget#controlPanel { background: #f3f6fa; }
                QFrame#panelHeader {
                    background: #ffffff;
                    border: 1px solid #dbe3ec;
                    border-radius: 10px;
                }
                QLabel#panelTitle { color: #0f172a; font-size: 18px; font-weight: 700; }
                QLabel#panelSubtitle { color: #64748b; font-size: 11px; }
                QLabel#telemetryBadge {
                    background: #e2e8f0;
                    color: #475569;
                    border: 1px solid #cbd5e1;
                    border-radius: 9px;
                    padding: 5px 8px;
                    font-size: 10px;
                    font-weight: 700;
                }
                QLabel#statusText {
                    color: #0f172a;
                    font-family: Consolas, 'Courier New', monospace;
                    font-size: 10px;
                    line-height: 1.15;
                }
                QLabel#lastActionText { color: #334155; font-size: 11px; }
                QLabel#runtimeText {
                    color: #64748b;
                    font-family: Consolas, 'Courier New', monospace;
                    font-size: 9px;
                }
                QLineEdit {
                    background: #ffffff;
                    color: #0f172a;
                    border: 1px solid #cbd5e1;
                    border-radius: 6px;
                    padding: 6px 8px;
                    selection-background-color: #bfdbfe;
                }
                QLineEdit:focus { border: 1px solid #3b82f6; }
                QTabWidget#settingsTabs::pane {
                    background: #ffffff;
                    border: 1px solid #dbe3ec;
                    border-radius: 8px;
                    top: -1px;
                }
                QTabBar::tab {
                    background: #e9eef5;
                    color: #475569;
                    border: 1px solid #dbe3ec;
                    padding: 7px 16px;
                    min-width: 72px;
                    font-weight: 600;
                }
                QTabBar::tab:selected { background: #ffffff; color: #0f172a; }
                QScrollArea { background: transparent; border: none; }
                QSplitter::handle { background: #dbe3ec; width: 2px; }
                QWidget#collapsibleSection { background: transparent; }
                QToolButton#sectionToggle {
                    background: #ffffff;
                    color: #334155;
                    border: 1px solid #dbe3ec;
                    border-radius: 8px;
                    padding: 9px 10px;
                    text-align: left;
                    font-size: 11px;
                    font-weight: 700;
                }
                QToolButton#sectionToggle:hover {
                    background: #f8fafc;
                    border-color: #94a3b8;
                }
                QFrame#sectionBody {
                    background: #ffffff;
                    border-left: 1px solid #dbe3ec;
                    border-right: 1px solid #dbe3ec;
                    border-bottom: 1px solid #dbe3ec;
                    border-bottom-left-radius: 8px;
                    border-bottom-right-radius: 8px;
                }
                QLabel#modeStatus {
                    color: #334155;
                    background: #f8fafc;
                    border: 1px solid #e2e8f0;
                    border-radius: 6px;
                    padding: 6px 8px;
                    font-weight: 600;
                }
                QLabel#modeHelp { color: #64748b; font-size: 9px; }
                """
            )

        def _build_ui(self) -> None:
            central = QtWidgets.QWidget(self)
            central.setObjectName("mainSurface")
            central.setStyleSheet("QWidget#mainSurface {background:#eef2f7;}")
            self.setCentralWidget(central)
            outer = QtWidgets.QVBoxLayout(central)
            outer.setContentsMargins(10, 10, 10, 10)
            outer.setSpacing(8)

            self.phase_banner = QtWidgets.QLabel("STARTING")
            self.phase_banner.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.phase_banner.setMinimumHeight(48)
            self.phase_banner.setStyleSheet(
                "QLabel {background:#e2e8f0; color:#0f172a; border:1px solid #cbd5e1; "
                "border-radius:10px; font-size:22px; font-weight:700; padding:8px;}"
            )
            outer.addWidget(self.phase_banner)

            splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
            splitter.setChildrenCollapsible(False)
            splitter.setHandleWidth(3)
            outer.addWidget(splitter, 1)

            plot_host = QtWidgets.QWidget()
            plot_grid = QtWidgets.QGridLayout(plot_host)
            plot_grid.setContentsMargins(0, 0, 4, 0)
            plot_grid.setHorizontalSpacing(8)
            plot_grid.setVerticalSpacing(8)

            plot_defs = [
                ("ck1_thickness", "CK-1 QMB thickness", "Thickness (Å)", "#15803d", False),
                ("ck1_rate", "CK-1 QMB rate", "Rate (Å/s)", "#15803d", False),
                ("pressure", "Chamber pressure", "Pressure (mbar)", "#2563eb", True),
                ("sample_thickness", "Sample QMB thickness", "Thickness (Å)", "#7c3aed", False),
                ("sample_rate", "Sample QMB rate", "Rate (Å/s)", "#7c3aed", False),
                ("temperature_view", "Oven PID temperature", "Temperature (ºC)", "#c62828", False),
                ("current", "Evaporator current", "Current (A)", "#d97706", False),
                ("voltage", "Evaporator voltage", "Voltage (V)", "#d97706", False),
                ("ck1_temperature", "CK-1 crucible temperature", "Temperature (ºC)", "#dc2626", False),
            ]

            for index, (key, title, ylabel, color, log_y) in enumerate(plot_defs):
                row, col = divmod(index, 3)
                axis = TimeAxis(orientation="bottom")
                value_axis = RawValueAxis(orientation="left")
                if key == "pressure":
                    # Reserve a stable lane for the logarithmic decade labels.
                    # Without this explicit width, the third-column pressure
                    # values can be clipped/covered when the control splitter is
                    # resized on the chamber PC.
                    value_axis.setWidth(104)
                    value_axis.setStyle(
                        autoExpandTextSpace=False,
                        tickTextWidth=72,
                        tickTextOffset=6,
                    )
                widget = pg.PlotWidget(axisItems={"bottom": axis, "left": value_axis})
                widget.setBackground("#ffffff")
                widget.setTitle(title, color="#0f172a", size="11pt")
                widget.setLabel("left", ylabel, color="#475569")
                # Show the measured values themselves on the y-axis.  PyQtGraph
                # otherwise applies an automatic SI scale factor (for example
                # x0.0001 / x10^-4), which makes live chamber readings harder
                # to read and compare with the instrument displays.
                widget.getAxis("left").enableAutoSIPrefix(False)
                widget.getAxis("left").setScale(1.0)
                widget.showGrid(x=True, y=True, alpha=0.12)
                widget.setStyleSheet("border:1px solid #dbe3ec; border-radius:8px;")
                widget.setMouseEnabled(x=True, y=True)
                widget.setMenuEnabled(True)
                widget.getPlotItem().setClipToView(True)
                widget.getPlotItem().setDownsampling(auto=True, mode="peak")
                if log_y:
                    widget.setLogMode(x=False, y=True)
                curve = widget.plot([], [], pen=pg.mkPen(color, width=2))
                curve.setClipToView(True)
                curve.setDownsampling(auto=True, method="peak")
                self._plots[key] = widget
                self._curves[key] = curve
                self._curve_has_data[key] = False
                plot_grid.addWidget(widget, row, col)

            self._reference_lines["rate_target"] = pg.InfiniteLine(
                angle=0, pen=pg.mkPen("#111827", width=1)
            )
            self._reference_lines["rate_low"] = pg.InfiniteLine(
                angle=0,
                pen=pg.mkPen("#94a3b8", width=1, style=QtCore.Qt.PenStyle.DotLine),
            )
            self._reference_lines["rate_high"] = pg.InfiniteLine(
                angle=0,
                pen=pg.mkPen("#94a3b8", width=1, style=QtCore.Qt.PenStyle.DotLine),
            )
            self._reference_lines["temp_target"] = pg.InfiniteLine(
                angle=0,
                pen=pg.mkPen("#111827", width=1, style=QtCore.Qt.PenStyle.DashLine),
            )
            for name in ("rate_target", "rate_low", "rate_high"):
                self._plots["ck1_rate"].addItem(self._reference_lines[name], ignoreBounds=True)
            self._plots["ck1_temperature"].addItem(
                self._reference_lines["temp_target"], ignoreBounds=True
            )

            # One persistent vertical marker per graph.  The phase supplies the
            # exact operator-confirmation timestamp; keeping the marker in the
            # shared dashboard guarantees identical behaviour in Phase 01/03.
            for key, widget in self._plots.items():
                shutter_line = pg.InfiniteLine(
                    angle=90,
                    movable=False,
                    pen=pg.mkPen(
                        "#0f766e", width=1.6, style=QtCore.Qt.PenStyle.DashLine
                    ),
                )
                shutter_line.setVisible(False)
                widget.addItem(shutter_line, ignoreBounds=True)
                self._shutter_reference_lines[key] = shutter_line

                shutter_close_line = pg.InfiniteLine(
                    angle=90,
                    movable=False,
                    pen=pg.mkPen(
                        "#c2410c", width=1.6, style=QtCore.Qt.PenStyle.DashLine
                    ),
                )
                shutter_close_line.setVisible(False)
                widget.addItem(shutter_close_line, ignoreBounds=True)
                self._shutter_close_reference_lines[key] = shutter_close_line

            splitter.addWidget(plot_host)
            splitter.addWidget(self._build_control_scroll())
            splitter.setStretchFactor(0, 7)
            splitter.setStretchFactor(1, 3)
            # Give controls enough room to scan comfortably without letting the
            # 3x3 plot matrix dominate the operator's attention.
            splitter.setSizes([1310, 540])

        def _build_control_scroll(self):
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMinimumWidth(440)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            panel = QtWidgets.QWidget()
            panel.setObjectName("controlPanel")
            layout = QtWidgets.QVBoxLayout(panel)
            layout.setContentsMargins(10, 6, 10, 10)
            layout.setSpacing(8)

            header = QtWidgets.QFrame()
            header.setObjectName("panelHeader")
            header_layout = QtWidgets.QHBoxLayout(header)
            header_layout.setContentsMargins(12, 10, 12, 10)
            title_stack = QtWidgets.QVBoxLayout()
            title_stack.setSpacing(2)
            heading = QtWidgets.QLabel(spec.phase_name)
            heading.setObjectName("panelTitle")
            subtitle = QtWidgets.QLabel("Live chamber control and monitoring")
            subtitle.setObjectName("panelSubtitle")
            title_stack.addWidget(heading)
            title_stack.addWidget(subtitle)
            header_layout.addLayout(title_stack, 1)
            self.telemetry_badge = QtWidgets.QLabel("CONNECTING")
            self.telemetry_badge.setObjectName("telemetryBadge")
            self.telemetry_badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.telemetry_badge.setMinimumWidth(94)
            header_layout.addWidget(self.telemetry_badge)
            layout.addWidget(header)

            # Phase-02-style expandable cards keep the default view compact.
            status_host = QtWidgets.QWidget()
            status_host_layout = QtWidgets.QVBoxLayout(status_host)
            status_host_layout.setContentsMargins(0, 0, 0, 0)
            status_host_layout.setSpacing(8)
            status_box = self._group("Process status", "#ffffff", "#dbe3ec")
            status_layout = QtWidgets.QVBoxLayout(status_box)
            self.status_label = QtWidgets.QLabel("Waiting for live telemetry…")
            self.status_label.setWordWrap(True)
            self.status_label.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.status_label.setObjectName("statusText")
            status_layout.addWidget(self.status_label)
            status_host_layout.addWidget(status_box)

            action_box = self._group("Last action", "#ffffff", "#dbe3ec")
            action_layout = QtWidgets.QVBoxLayout(action_box)
            self.last_action_label = QtWidgets.QLabel("No operator action yet.")
            self.last_action_label.setWordWrap(True)
            self.last_action_label.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.last_action_label.setObjectName("lastActionText")
            action_layout.addWidget(self.last_action_label)
            status_host_layout.addWidget(action_box)
            self.status_section = CollapsibleSection(
                "Live process status", status_host, expanded=False
            )

            actions_host = QtWidgets.QWidget()
            actions = QtWidgets.QGridLayout(actions_host)
            actions.setContentsMargins(0, 0, 0, 0)
            actions.setHorizontalSpacing(8)
            actions.setVerticalSpacing(8)
            action_defs = (
                (
                    "Open shutter",
                    lambda: self._dispatch("Confirm shutter open", spec.open_shutter),
                    "#dcfce7",
                    "#166534",
                    0,
                    0,
                ),
                (
                    "Close shutter",
                    lambda: self._dispatch("Confirm shutter closed", spec.close_shutter),
                    "#ffedd5",
                    "#9a3412",
                    0,
                    1,
                ),
                ("Abort / safe stop", self._abort, "#fee2e2", "#991b1b", 1, 0),
                (
                    "Finish phase",
                    lambda: self._dispatch("Finish phase", spec.finish),
                    "#e0f2fe",
                    "#075985",
                    1,
                    1,
                ),
            )
            self.action_buttons: dict[str, QtWidgets.QPushButton] = {}
            for button_text, callback, bg, fg, row, col in action_defs:
                button = self._button(button_text, bg, fg)
                button.setMinimumHeight(42)
                button.clicked.connect(callback)
                actions.addWidget(button, row, col)
                self.action_buttons[button_text] = button
            self.actions_section = CollapsibleSection(
                "Operator controls", actions_host, expanded=True
            )
            # Sections are inserted after construction in the operator-first
            # order defined at the end of this method.

            view_host = QtWidgets.QWidget()
            view_layout = QtWidgets.QHBoxLayout(view_host)
            view_layout.setContentsMargins(0, 0, 0, 0)
            view_layout.setSpacing(6)
            self.temperature_buttons: dict[str, QtWidgets.QPushButton] = {}
            for mode, button_text, bg, fg in (
                ("oven", "Oven PID", "#fde8e8", "#c62828"),
                ("pyrometer", "Pyrometer", "#e3f0ff", "#1565c0"),
                ("sample", "Sample est.", "#fff8cf", "#9a7600"),
            ):
                button = self._button(button_text, bg, fg)
                button.setCheckable(True)
                button.clicked.connect(
                    lambda _checked=False, selected=mode: self._set_temperature_view(selected)
                )
                view_layout.addWidget(button)
                self.temperature_buttons[mode] = button
            self.view_section = CollapsibleSection(
                "Temperature graph selector", view_host, expanded=False
            )

            settings_tabs = QtWidgets.QTabWidget()
            settings_tabs.setObjectName("settingsTabs")
            settings_tabs.setDocumentMode(True)

            targets_page = QtWidgets.QWidget()
            targets_layout = QtWidgets.QVBoxLayout(targets_page)
            targets_layout.setContentsMargins(12, 10, 12, 12)
            targets_layout.setSpacing(10)

            self.feedback_mode_status = QtWidgets.QLabel("Active controller: connecting…")
            self.feedback_mode_status.setObjectName("modeStatus")
            self.feedback_mode_status.setWordWrap(True)
            targets_layout.addWidget(self.feedback_mode_status)

            mode_host = QtWidgets.QWidget()
            mode_layout = QtWidgets.QVBoxLayout(mode_host)
            mode_layout.setContentsMargins(0, 0, 0, 0)
            mode_layout.setSpacing(5)
            self.feedback_mode_group = QtWidgets.QButtonGroup(self)
            self.feedback_mode_group.setExclusive(True)
            self.feedback_mode_buttons: dict[str, QtWidgets.QRadioButton] = {}
            for mode, button_text in (
                ("temperature", "Temperature PID"),
                ("rate", "Rate PID"),
                ("compound", "Compound cascade"),
            ):
                radio = QtWidgets.QRadioButton(button_text)
                radio.toggled.connect(self._mark_feedback_mode_dirty)
                self.feedback_mode_group.addButton(radio)
                mode_layout.addWidget(radio)
                self.feedback_mode_buttons[mode] = radio
            targets_layout.addWidget(mode_host)

            mode_help = QtWidgets.QLabel(
                "A controller change is applied explicitly and keeps the present "
                "Keysight current. The new loop starts with a bumpless settling handover."
            )
            mode_help.setObjectName("modeHelp")
            mode_help.setWordWrap(True)
            targets_layout.addWidget(mode_help)
            apply_mode_button = self._button(
                "Apply feedback controller", "#e0f2fe", "#075985"
            )
            apply_mode_button.clicked.connect(self._apply_feedback_mode)
            if spec.set_feedback_mode is None:
                apply_mode_button.setEnabled(False)
                apply_mode_button.setToolTip("Live mode switching is unavailable in this runtime.")
            targets_layout.addWidget(apply_mode_button)

            targets_form = QtWidgets.QFormLayout()
            self.target_temp = self._line_edit()
            self.target_rate = self._line_edit()
            self.pid_band = self._line_edit()
            self.target_temp.setToolTip("Live temperature target / guide. Press Enter or Apply targets during a run.")
            self.target_rate.setToolTip("Live CK-1 rate target. Press Enter or Apply targets during a run.")
            self.pid_band.setToolTip("Live inner temperature-PID dead band.")
            targets_form.addRow("Live T target / guide (ºC)", self.target_temp)
            targets_form.addRow("Live CK-1 rate target (Å/s)", self.target_rate)
            targets_form.addRow("PID band (ºC)", self.pid_band)
            targets_layout.addLayout(targets_form)

            self.target_context_help = QtWidgets.QLabel("")
            self.target_context_help.setObjectName("targetContextHelp")
            self.target_context_help.setWordWrap(True)
            targets_layout.addWidget(self.target_context_help)
            for key, edit in (
                ("temp", self.target_temp),
                ("rate", self.target_rate),
                ("band", self.pid_band),
            ):
                edit.textEdited.connect(
                    lambda _text, field_key=key: self._mark_target_edit_dirty(field_key)
                )
                edit.returnPressed.connect(self._apply_targets)

            target_button_host = QtWidgets.QWidget()
            target_button_row = QtWidgets.QHBoxLayout(target_button_host)
            target_button_row.setContentsMargins(0, 0, 0, 0)
            self.apply_targets_button = self._button(
                "Apply targets", "#dbeafe", "#1e3a8a"
            )
            self.apply_targets_button.clicked.connect(self._apply_targets)
            target_button_row.addWidget(self.apply_targets_button)
            reset_targets_button = self._button(
                "Reset targets", "#e2e8f0", "#334155"
            )
            reset_targets_button.clicked.connect(self._reset_targets)
            target_button_row.addWidget(reset_targets_button)
            targets_layout.addWidget(target_button_host)
            settings_tabs.addTab(targets_page, "Control")

            ramp_page = QtWidgets.QWidget()
            ramp_layout = QtWidgets.QVBoxLayout(ramp_page)
            ramp_layout.setContentsMargins(12, 10, 12, 12)
            mode_row = QtWidgets.QHBoxLayout()
            self.steps_mode = QtWidgets.QRadioButton("Steps mode")
            self.slope_mode = QtWidgets.QRadioButton("Slope mode")
            mode_row.addWidget(self.steps_mode)
            mode_row.addWidget(self.slope_mode)
            mode_row.addStretch(1)
            ramp_layout.addLayout(mode_row)
            ramp_form = QtWidgets.QFormLayout()
            self.steps_until = self._line_edit()
            self.step_period = self._line_edit()
            self.slope_early = self._line_edit()
            self.slope_mid = self._line_edit()
            self.slope_late = self._line_edit()
            ramp_form.addRow("Steps until T (ºC)", self.steps_until)
            ramp_form.addRow("Step period (s)", self.step_period)
            ramp_form.addRow("Slope early (ºC/min)", self.slope_early)
            ramp_form.addRow("Slope mid (ºC/min)", self.slope_mid)
            ramp_form.addRow("Slope late (ºC/min)", self.slope_late)
            ramp_layout.addLayout(ramp_form)
            ramp_layout.addWidget(
                self._button_row(
                    ("Apply ramp", self._apply_ramp, "#ede9fe", "#5b21b6"),
                    (
                        "Reset ramp",
                        lambda: self._dispatch("Reset ramp settings", spec.reset_ramp),
                        "#f1f5f9",
                        "#334155",
                    ),
                )
            )
            settings_tabs.addTab(ramp_page, "Ramp")

            manual_page = QtWidgets.QWidget()
            manual_form = QtWidgets.QFormLayout(manual_page)
            manual_form.setContentsMargins(12, 12, 12, 12)
            self.manual_current = self._line_edit()
            manual_form.addRow("Manual I (A)", self.manual_current)
            manual_form.addRow(
                self._button_row(
                    ("Set manual current", self._set_manual_current, "#fee2e2", "#991b1b"),
                    (
                        "Resume automatic",
                        lambda: self._dispatch(
                            "Resume automatic current", spec.resume_automatic_current
                        ),
                        "#dcfce7",
                        "#166534",
                    ),
                )
            )
            settings_tabs.addTab(manual_page, "Manual")
            self.settings_section = CollapsibleSection(
                "Editable targets and controller", settings_tabs, expanded=True
            )

            runtime_host = QtWidgets.QWidget()
            runtime_layout = QtWidgets.QVBoxLayout(runtime_host)
            runtime_layout.setContentsMargins(0, 0, 0, 0)
            self.runtime_health_label = QtWidgets.QLabel("Starting runtime threads…")
            self.runtime_health_label.setWordWrap(True)
            self.runtime_health_label.setObjectName("runtimeText")
            runtime_layout.addWidget(self.runtime_health_label)
            self.runtime_section = CollapsibleSection(
                "GUI runtime health", runtime_host, expanded=False
            )

            # Operator-facing hierarchy for Phase 01 / 03.  The two sections
            # used to run and tune the experiment are open immediately;
            # selectors and live status stay available without competing for
            # vertical space during normal operation.
            layout.addWidget(self.actions_section)
            layout.addWidget(self.settings_section)
            layout.addWidget(self.view_section)
            layout.addWidget(self.status_section)
            layout.addWidget(self.runtime_section)
            layout.addStretch(1)

            scroll.setWidget(panel)
            return scroll

        @staticmethod
        def _group(title: str, background: str, border: str):
            box = QtWidgets.QGroupBox(title)
            box.setStyleSheet(
                "QGroupBox {font-weight:700; color:#334155; border:1px solid "
                + border
                + "; border-radius:8px; margin-top:10px; padding-top:8px; background:"
                + background
                + ";} QGroupBox::title {subcontrol-origin:margin; left:10px; padding:0 4px;}"
            )
            return box

        @staticmethod
        def _line_edit():
            edit = QtWidgets.QLineEdit()
            edit.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
            # Avoid Qt's locale-dependent floating-point validator here.
            # In es-ES/ca-ES, a dot may be treated as a thousands separator, so
            # typing 0.2 can be normalized to 02 -> 2.0.  This locale-neutral
            # validator accepts either decimal convention and forbids grouping.
            decimal_pattern = QtCore.QRegularExpression(
                r"^[+-]?(?:(?:\d+(?:[\.,]\d*)?)|(?:[\.,]\d+))(?:[eE][+-]?\d+)?$"
            )
            edit.setValidator(QtGui.QRegularExpressionValidator(decimal_pattern, edit))
            edit.setMinimumWidth(105)
            return edit

        @staticmethod
        def _button(text: str, background: str, foreground: str):
            button = QtWidgets.QPushButton(text)
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{background:{background}; color:{foreground}; border:1px solid #cbd5e1; "
                "border-radius:6px; padding:7px; font-weight:600;} "
                "QPushButton:hover {border:1px solid #64748b;} "
                "QPushButton:disabled {background:#e2e8f0; color:#94a3b8;} "
                "QPushButton:pressed {padding-top:8px; padding-left:8px;}"
            )
            return button

        def _button_row(self, *defs):
            host = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(host)
            row.setContentsMargins(0, 0, 0, 0)
            for text, callback, background, foreground in defs:
                button = self._button(text, background, foreground)
                button.clicked.connect(callback)
                row.addWidget(button)
            return host

        # ---------- Operator actions ----------
        @staticmethod
        def _float(edit, label: str) -> float:
            return parse_locale_flexible_float(edit.text(), label)

        def _mark_feedback_mode_dirty(self, checked: bool) -> None:
            if checked:
                self._feedback_mode_dirty = True
                self._update_target_context(self._selected_feedback_mode())

        def _mark_target_edit_dirty(self, field_key: str) -> None:
            """Keep unsubmitted target edits stable while telemetry continues to refresh."""
            if field_key in self._target_edit_dirty:
                self._target_edit_dirty[field_key] = True
            self._refresh_target_apply_button()

        def _refresh_target_apply_button(self) -> None:
            dirty_count = sum(1 for dirty in self._target_edit_dirty.values() if dirty)
            if dirty_count:
                suffix = "change" if dirty_count == 1 else "changes"
                self.apply_targets_button.setText(f"Apply targets · {dirty_count} {suffix}")
                self.apply_targets_button.setToolTip(
                    "These edits are staged locally. They will not be sent to the controller "
                    "until you press this button or Enter."
                )
            else:
                self.apply_targets_button.setText("Apply targets")
                self.apply_targets_button.setToolTip("")

        def _stage_pending_target_values(self, values: tuple[float, float, float]) -> None:
            self._pending_target_values = {
                "temp": float(values[0]),
                "rate": float(values[1]),
                "band": float(values[2]),
            }

        def _reset_targets(self) -> None:
            accepted = self._dispatch(
                "Reset heating targets", spec.reset_targets, allow_duplicate=False
            )
            if accepted:
                self._pending_target_values = None
                for key in self._target_edit_dirty:
                    self._target_edit_dirty[key] = False
                self._refresh_target_apply_button()

        def _update_target_context(self, mode: str | None) -> None:
            """Explain and visually emphasize the live target used by each controller."""
            mode = str(mode or "").strip().lower()
            neutral = "QLineEdit {background:#ffffff;}"
            active = "QLineEdit {background:#eff6ff; border:1px solid #60a5fa;}"
            secondary = "QLineEdit {background:#fffbeb; border:1px solid #fbbf24;}"
            self.target_temp.setStyleSheet(neutral)
            self.target_rate.setStyleSheet(neutral)
            if mode == "temperature":
                self.target_temp.setStyleSheet(active)
                text = (
                    "Temperature PID uses the live T target directly. The rate target remains "
                    "editable as the shutter-rate threshold and for next-mode handover."
                )
            elif mode == "rate":
                self.target_rate.setStyleSheet(active)
                text = (
                    "Rate PID uses the live CK-1 rate target directly. The temperature target "
                    "remains a live process guide/safety reference."
                )
            elif mode == "compound":
                self.target_rate.setStyleSheet(active)
                self.target_temp.setStyleSheet(secondary)
                text = (
                    "Compound cascade uses the live CK-1 rate target as the outer-loop target; "
                    "the live temperature target remains the cascade base/guide."
                )
            else:
                text = "Live targets can be changed at any time during Phase 01/03."
            self.target_context_help.setText(text)

        def _selected_feedback_mode(self) -> str | None:
            for mode, button in self.feedback_mode_buttons.items():
                if button.isChecked():
                    return mode
            return None

        def _target_values(self) -> tuple[float, float, float]:
            return (
                self._float(self.target_temp, "Temperature target"),
                self._float(self.target_rate, "Rate target"),
                self._float(self.pid_band, "PID band"),
            )

        def _apply_feedback_mode(self) -> None:
            if spec.set_feedback_mode is None:
                self._show_action_error("Live feedback-mode switching is unavailable.")
                return
            mode = self._selected_feedback_mode()
            if mode is None:
                self._show_action_error("Select Temperature PID, Rate PID or Compound cascade.")
                return
            try:
                target_values = self._target_values()
            except Exception as exc:
                self._show_action_error(str(exc))
                return
            label = {
                "temperature": "Temperature PID",
                "rate": "Rate PID",
                "compound": "Compound cascade",
            }.get(mode, mode)

            def apply_mode_with_live_targets() -> None:
                # One serialized operator command prevents a mode handover from
                # starting with stale targets.  Existing current is still held by
                # the phase's established bumpless set_evaporation_control_mode().
                spec.apply_targets(*target_values)
                spec.set_feedback_mode(mode)

            accepted = self._dispatch(
                f"Apply {label} + live targets",
                apply_mode_with_live_targets,
                allow_duplicate=False,
            )
            if accepted:
                self._stage_pending_target_values(target_values)
                self.feedback_mode_status.setText(
                    f"Controller + live targets requested: {label}. Waiting for telemetry confirmation…"
                )

        def _apply_targets(self) -> None:
            try:
                values = self._target_values()
            except Exception as exc:
                self._show_action_error(str(exc))
                return
            accepted = self._dispatch(
                "Apply live heating targets", spec.apply_targets, *values
            )
            if accepted:
                self._stage_pending_target_values(values)

        def _apply_ramp(self) -> None:
            try:
                mode = "steps" if self.steps_mode.isChecked() else "slope"
                values = (
                    mode,
                    self._float(self.steps_until, "Steps-until temperature"),
                    self._float(self.step_period, "Step period"),
                    self._float(self.slope_early, "Early slope"),
                    self._float(self.slope_mid, "Mid slope"),
                    self._float(self.slope_late, "Late slope"),
                )
            except Exception as exc:
                self._show_action_error(str(exc))
                return
            self._dispatch("Apply ramp settings", spec.apply_ramp, *values)

        def _set_manual_current(self) -> None:
            try:
                value = self._float(self.manual_current, "Manual current")
            except Exception as exc:
                self._show_action_error(str(exc))
                return
            self._dispatch("Set manual current", spec.set_manual_current, value)

        def _set_temperature_view(self, mode: str) -> None:
            self._dispatch(
                f"Select {mode} temperature view", spec.set_temperature_view, mode
            )

        def _abort(self) -> None:
            if self._abort_dispatched:
                return
            self._abort_dispatched = True
            self.phase_banner.setText("ABORT REQUESTED · SAFE STOP IN PROGRESS")
            self.phase_banner.setStyleSheet(
                "QLabel {background:#fee2e2; color:#991b1b; border:2px solid #ef4444; "
                "border-radius:10px; font-size:22px; font-weight:700; padding:8px;}"
            )
            for button in self.action_buttons.values():
                button.setEnabled(False)
            accepted = self._dispatch(
                "Abort / safe stop",
                spec.abort,
                priority=0,
                allow_duplicate=False,
            )
            if not accepted:
                self._abort_dispatched = False
                for button in self.action_buttons.values():
                    button.setEnabled(True)

        def _dispatch(
            self,
            label: str,
            callback: Callable[..., None],
            *args: Any,
            priority: int = 10,
            allow_duplicate: bool = False,
        ) -> bool:
            """Queue one deterministic command outside the Qt event loop."""

            if self._runtime_shutdown:
                self._show_action_error("GUI runtime is already shutting down.")
                return False
            if not allow_duplicate and label in self._pending_command_labels:
                self.last_action_label.setText(f"Already queued or running: {label}")
                self.last_action_label.setStyleSheet("color:#9a3412; font-weight:600;")
                return False
            accepted = self._command_thread.submit(
                label, callback, tuple(args), priority=priority
            )
            if not accepted:
                self._show_action_error(
                    f"Command queue is unavailable or full; action not sent: {label}"
                )
                return False
            self._pending_command_labels.add(label)
            self._command_state = "queued"
            self.last_action_label.setText(f"Queued: {label}")
            self.last_action_label.setStyleSheet("color:#075985; font-weight:600;")
            return True

        @QtCore.Slot(str)
        def _command_started(self, label: str) -> None:
            self._command_state = "running"
            self.last_action_label.setText(f"Running: {label}")
            self.last_action_label.setStyleSheet("color:#075985; font-weight:700;")

        @QtCore.Slot(str)
        def _command_completed(self, label: str) -> None:
            self._pending_command_labels.discard(label)
            self._command_state = "idle" if not self._pending_command_labels else "queued"
            self.last_action_label.setText(f"Completed: {label}")
            self.last_action_label.setStyleSheet("color:#166534; font-weight:600;")

        @QtCore.Slot(str, str)
        def _command_failed(self, label: str, message: str) -> None:
            self._pending_command_labels.discard(label)
            self._command_state = "error"
            self._show_action_error(f"{label} failed: {message}")

        @QtCore.Slot(str)
        def _show_telemetry_error(self, message: str) -> None:
            self.runtime_health_label.setText(str(message))
            self.runtime_health_label.setStyleSheet(
                "font-family:Consolas, 'Courier New', monospace; color:#b91c1c; font-weight:700;"
            )

        def _show_action_error(self, message: str) -> None:
            self.last_action_label.setText(str(message))
            self.last_action_label.setStyleSheet("color:#b91c1c; font-weight:700;")

        # ---------- Mailbox-driven refresh ----------
        @staticmethod
        def _xy(
            times: list[Any],
            values: list[Any],
            max_points: int,
            positive_only: bool = False,
        ):
            pairs: list[tuple[float, float]] = []
            count = min(len(times), len(values))
            if count <= 0:
                return [], []
            start = max(0, count - max(1, max_points))
            for timestamp, value in zip(times[start:count], values[start:count]):
                try:
                    x = timestamp.timestamp() if hasattr(timestamp, "timestamp") else float(timestamp)
                    y = float(value)
                except Exception:
                    continue
                if not (math.isfinite(x) and math.isfinite(y)):
                    continue
                if positive_only and y <= 0:
                    continue
                pairs.append((x, y))
            if not pairs:
                return [], []
            xs, ys = zip(*pairs)
            return list(xs), list(ys)

        @staticmethod
        def _latest(values: list[Any]) -> float | None:
            for value in reversed(values):
                try:
                    number = float(value)
                except Exception:
                    continue
                if math.isfinite(number):
                    return number
            return None

        @staticmethod
        def _range_with_padding(values: list[float], key: str) -> tuple[float, float] | None:
            finite = [float(value) for value in values if math.isfinite(float(value))]
            if not finite:
                return None
            low = min(finite)
            high = max(finite)
            span = high - low
            minimum_span = {
                "ck1_thickness": 0.20,
                "sample_thickness": 0.20,
                "ck1_rate": 0.10,
                "sample_rate": 0.10,
                "temperature_view": 10.0,
                "ck1_temperature": 10.0,
                "current": 0.020,
                "voltage": 0.050,
            }.get(key, 1.0)
            if span < minimum_span:
                centre = (low + high) / 2.0
                span = minimum_span
                low = centre - span / 2.0
                high = centre + span / 2.0
            margin = max(span * 0.12, minimum_span * 0.08)
            low -= margin
            high += margin
            if key in {"current", "voltage"} and low < 0.0:
                low = 0.0
            return low, high

        def _apply_adaptive_live_fit(
            self, key: str, xs: list[float], ys: list[float], *, force: bool = False
        ) -> None:
            """Keep live plots legible without requiring repeated operator clicks.

            The displayed x-range follows the newest retained samples.  Linear
            y-axes use a padded range with hysteresis so they do not visibly
            breathe on every telemetry frame.  Pressure remains on pyqtgraph's
            native log auto-range.
            """

            if not xs or not ys:
                return
            plot = self._plots[key]
            x_low = min(xs)
            x_high = max(xs)
            marker = self._shutter_open_timestamp
            if marker is not None and math.isfinite(marker):
                # Preserve the operator reference even when a thickness trace
                # has just restarted and only contains post-shutter points.
                x_low = min(x_low, marker)
                x_high = max(x_high, marker)
            close_marker = self._shutter_close_timestamp
            if close_marker is not None and math.isfinite(close_marker):
                x_low = min(x_low, close_marker)
                x_high = max(x_high, close_marker)
            if math.isclose(x_low, x_high, abs_tol=1e-9):
                plot.setXRange(x_low - 30.0, x_high + 30.0, padding=0.0)
            else:
                plot.setXRange(x_low, x_high, padding=0.015)

            if key == "pressure":
                # Use pyqtgraph's public auto-range path for the logarithmic
                # pressure axis; manual linear limits would be incorrect here.
                plot.enableAutoRange(axis="y", enable=True)
                plot.getViewBox().autoRange(padding=0.06)
                return

            desired = self._range_with_padding(ys, key)
            if desired is None:
                return
            desired_low, desired_high = desired
            previous = self._autoscale_ranges.get(key)
            should_update = force or previous is None
            if previous is not None and not should_update:
                old_low, old_high = previous
                old_span = max(old_high - old_low, 1e-12)
                latest = ys[-1]
                inner_low = old_low + 0.10 * old_span
                inner_high = old_high - 0.10 * old_span
                desired_span = desired_high - desired_low
                # Expand promptly when new data approaches an edge; shrink only
                # when the old range is substantially larger to avoid jitter.
                should_update = (
                    latest < inner_low
                    or latest > inner_high
                    or desired_low < old_low - 0.08 * old_span
                    or desired_high > old_high + 0.08 * old_span
                    or desired_span < 0.62 * old_span
                )
            if should_update:
                plot.setYRange(desired_low, desired_high, padding=0.0)
                self._autoscale_ranges[key] = (desired_low, desired_high)

        def _update_curve(
            self,
            key: str,
            times: list[Any],
            values: list[Any],
            title: str,
            unit: str,
            *,
            positive_only: bool = False,
            force_autoscale: bool = False,
        ) -> None:
            xs, ys = self._xy(
                times, values, spec.max_plot_points, positive_only=positive_only
            )
            self._curves[key].setData(xs, ys, connect="finite")
            if xs:
                self._curve_has_data[key] = True
                self._apply_adaptive_live_fit(
                    key, xs, ys, force=force_autoscale
                )
            latest = self._latest(values)
            if latest is None:
                suffix = "--"
            elif unit == "mbar":
                suffix = f"{latest:.2e} {unit}"
            elif unit in {"A", "V"}:
                suffix = f"{latest:.4f} {unit}" if unit == "A" else f"{latest:.3f} {unit}"
            elif unit == "Å/s":
                suffix = f"{latest:.3f} {unit}"
            elif unit == "Å":
                suffix = f"{latest:.2f} {unit}"
            else:
                suffix = f"{latest:.1f} {unit}"
            self._plots[key].setTitle(
                f"{title}  ·  <span style='color:#64748b'>{suffix}</span>",
                color="#0f172a",
                size="11pt",
            )

        def _refresh_from_mailbox(self) -> None:
            frame = self._mailbox.latest_after(self._last_frame_sequence)
            if frame is None:
                return

            if self._last_frame_sequence:
                self._dropped_frames += max(
                    0, frame.sequence - self._last_frame_sequence - 1
                )
            self._last_frame_sequence = frame.sequence
            self._last_frame_monotonic = frame.captured_monotonic
            self._last_poll_duration_s = frame.poll_duration_s

            render_started = time.monotonic()
            try:
                self._render_frame(frame.snapshot, frame.status)
            except Exception as exc:
                self._show_action_error(f"GUI render failed: {exc}")
            finally:
                self._last_render_duration_s = time.monotonic() - render_started

        def _render_frame(self, snapshot: Snapshot, status: Status) -> None:
            self._last_status = status
            ck1 = snapshot.get("CK-1 evaporator QMB", {})
            sample = snapshot.get("Sample QMB", {})
            pressure = snapshot.get("XGS600 HFIG pressure", {})
            oven = snapshot.get("Oven PID temperature", {})
            pyro = snapshot.get("IMPAC pyrometer", {})
            supply = snapshot.get("Keysight power supply", {})
            arduino = snapshot.get("Arduino CK-1 crucible temperature", {})

            self._update_shutter_reference(status)

            ck1_thickness_times = ck1.get("thickness_times", [])
            ck1_thickness_values = ck1.get("thickness_data", [])
            sample_thickness_times = sample.get("thickness_times", [])
            sample_thickness_values = sample.get("thickness_data", [])
            ck1_thickness_title = "CK-1 QMB thickness"
            sample_thickness_title = "Sample QMB thickness"
            if self._relative_thickness_active:
                ck1_thickness_times, ck1_thickness_values = relative_thickness_series(
                    ck1_thickness_times,
                    ck1_thickness_values,
                    baseline=self._relative_thickness_baselines.get("ck1"),
                    start_timestamp=self._shutter_open_timestamp,
                )
                sample_thickness_times, sample_thickness_values = relative_thickness_series(
                    sample_thickness_times,
                    sample_thickness_values,
                    baseline=self._relative_thickness_baselines.get("sample"),
                    start_timestamp=self._shutter_open_timestamp,
                )
                ck1_thickness_title = "CK-1 QMB relative thickness"
                sample_thickness_title = "Sample QMB relative thickness"

            self._update_curve(
                "ck1_thickness",
                ck1_thickness_times,
                ck1_thickness_values,
                ck1_thickness_title,
                "Å",
            )
            self._update_curve(
                "ck1_rate",
                ck1.get("rate_times", []),
                ck1.get("rate_data", []),
                "CK-1 QMB rate",
                "Å/s",
            )
            self._update_curve(
                "pressure",
                pressure.get("pressure_times", []),
                pressure.get("pressure_data", []),
                "Chamber pressure",
                "mbar",
                positive_only=True,
            )
            self._update_curve(
                "sample_thickness",
                sample_thickness_times,
                sample_thickness_values,
                sample_thickness_title,
                "Å",
            )
            self._update_curve(
                "sample_rate",
                sample.get("rate_times", []),
                sample.get("rate_data", []),
                "Sample QMB rate",
                "Å/s",
            )

            view = str(status.get("temperature_view", "oven"))
            temperature_view_changed = view != self._last_temperature_view
            if view == "pyrometer":
                temp_times = pyro.get("temperature_times", [])
                temp_values = pyro.get("temperature_data", [])
                temp_title, temp_color = "Raw pyrometer temperature", "#1565c0"
            elif view == "sample":
                temp_times = pyro.get("temperature_times", [])
                temp_values = pyro.get("sample_temperature_data", [])
                temp_title, temp_color = "Estimated sample temperature", "#d4a000"
            else:
                temp_times = oven.get("temperature_times", [])
                temp_values = oven.get("temperature_data", [])
                temp_title, temp_color = "Oven PID temperature", "#c62828"
            self._curves["temperature_view"].setPen(pg.mkPen(temp_color, width=2))
            self._update_curve(
                "temperature_view", temp_times, temp_values, temp_title, "ºC",
                force_autoscale=temperature_view_changed,
            )
            self._last_temperature_view = view

            self._update_curve(
                "current",
                supply.get("current_times", []),
                supply.get("current_data", []),
                "Evaporator current",
                "A",
            )
            self._update_curve(
                "voltage",
                supply.get("voltage_times", []),
                supply.get("voltage_data", []),
                "Evaporator voltage",
                "V",
            )
            self._update_curve(
                "ck1_temperature",
                arduino.get("temperature_times", []),
                arduino.get("temperature_data", []),
                "CK-1 crucible temperature",
                "ºC",
            )

            self._update_status(status)

        def _update_shutter_reference(self, status: Status) -> None:
            raw_timestamp = status.get("shutter_open_timestamp")
            timestamp: float | None
            try:
                timestamp = float(raw_timestamp) if raw_timestamp is not None else None
            except Exception:
                timestamp = None
            if timestamp is not None and not math.isfinite(timestamp):
                timestamp = None

            raw_close_timestamp = status.get("shutter_close_timestamp")
            close_timestamp: float | None
            try:
                close_timestamp = (
                    float(raw_close_timestamp) if raw_close_timestamp is not None else None
                )
            except Exception:
                close_timestamp = None
            if close_timestamp is not None and not math.isfinite(close_timestamp):
                close_timestamp = None

            self._shutter_open_timestamp = timestamp
            self._shutter_close_timestamp = close_timestamp
            self._relative_thickness_active = bool(
                status.get("relative_thickness_active", timestamp is not None)
            )
            self._relative_thickness_baselines = {
                "ck1": status.get("baseline_ck1_thickness"),
                "sample": status.get("baseline_sample_thickness"),
            }

            for line in self._shutter_reference_lines.values():
                if timestamp is None:
                    line.setVisible(False)
                else:
                    line.setValue(timestamp)
                    line.setVisible(True)

            for line in self._shutter_close_reference_lines.values():
                if close_timestamp is None:
                    line.setVisible(False)
                else:
                    line.setValue(close_timestamp)
                    line.setVisible(True)

        def _update_status(self, status: Status) -> None:
            phase = str(status.get("phase_label") or status.get("phase") or "STARTING")
            if phase != self._last_phase:
                self.phase_banner.setText(phase)
                safety = "SAFETY" in phase.upper() or "ABORT" in phase.upper()
                finished = "FINISHED" in phase.upper() or "HANDOFF" in phase.upper()
                if safety:
                    bg, border, fg = "#fee2e2", "#ef4444", "#991b1b"
                elif finished:
                    bg, border, fg = "#dcfce7", "#22c55e", "#166534"
                else:
                    bg, border, fg = "#e0f2fe", "#38bdf8", "#0c4a6e"
                self.phase_banner.setStyleSheet(
                    f"QLabel {{background:{bg}; color:{fg}; border:2px solid {border}; "
                    "border-radius:10px; font-size:22px; font-weight:700; padding:8px;}"
                )
                self._last_phase = phase

            lines = status.get("status_lines", [])
            status_text = "\n".join(str(line) for line in lines)
            if status_text != self._last_status_text:
                self.status_label.setText(status_text)
                self._last_status_text = status_text
            if self._command_state == "idle":
                last_action = str(status.get("last_action", "")).strip()
                if last_action and last_action != self._last_action_text:
                    self.last_action_label.setText(last_action)
                    self.last_action_label.setStyleSheet("color:#334155;")
                    self._last_action_text = last_action

            feedback_mode = str(status.get("feedback_mode", "temperature")).strip().lower()
            feedback_label = str(
                status.get("feedback_mode_label")
                or {
                    "temperature": "Temperature PID",
                    "rate": "Rate PID",
                    "compound": "Compound cascade",
                }.get(feedback_mode, feedback_mode)
            )
            active_controller = str(status.get("active_feedback_controller", "--"))
            mode_status_text = (
                f"Selected mode: {feedback_label}\nActive loop: {active_controller}"
            )
            if self.feedback_mode_status.text() != mode_status_text:
                self.feedback_mode_status.setText(mode_status_text)
            selected_mode = self._selected_feedback_mode()
            if selected_mode == feedback_mode:
                self._feedback_mode_dirty = False
            if not self._feedback_mode_dirty:
                for mode_key, button in self.feedback_mode_buttons.items():
                    button.blockSignals(True)
                    button.setChecked(mode_key == feedback_mode)
                    button.blockSignals(False)
            self._update_target_context(self._selected_feedback_mode() or feedback_mode)

            targets = status.get("targets", {})
            self._acknowledge_pending_targets(targets)
            self._set_target_if_clean(
                "temp", self.target_temp, targets.get("trigger_temp_c"), 1
            )
            self._set_target_if_clean(
                "rate", self.target_rate, targets.get("rate_target_a_per_s"), 3
            )
            self._set_target_if_clean(
                "band", self.pid_band, targets.get("pid_temp_band_c"), 2
            )
            if targets:
                self._reference_lines["temp_target"].setValue(
                    float(targets.get("trigger_temp_c", 0.0))
                )
                self._reference_lines["rate_target"].setValue(
                    float(targets.get("rate_target_a_per_s", 0.0))
                )
                self._reference_lines["rate_low"].setValue(
                    float(targets.get("rate_low_a_per_s", 0.0))
                )
                self._reference_lines["rate_high"].setValue(
                    float(targets.get("rate_high_a_per_s", 0.0))
                )

            ramp = status.get("ramp", {})
            mode = str(ramp.get("mode", "steps"))
            self.steps_mode.setChecked(mode == "steps")
            self.slope_mode.setChecked(mode == "slope")
            self._set_if_idle(self.steps_until, ramp.get("steps_until_temp_c"), 1)
            self._set_if_idle(self.step_period, ramp.get("steps_step_period_s"), 1)
            self._set_if_idle(self.slope_early, ramp.get("slope_early_c_per_min"), 2)
            self._set_if_idle(self.slope_mid, ramp.get("slope_mid_c_per_min"), 2)
            self._set_if_idle(self.slope_late, ramp.get("slope_late_c_per_min"), 2)

            manual = status.get("manual", {})
            self._set_if_idle(
                self.manual_current, manual.get("requested_current_a"), 3
            )

            view = str(status.get("temperature_view", "oven"))
            for key, button in self.temperature_buttons.items():
                button.setChecked(key == view)

        def _acknowledge_pending_targets(self, targets: dict[str, Any]) -> None:
            pending = self._pending_target_values
            if not pending or not targets:
                return
            live_values = {
                "temp": targets.get("trigger_temp_c"),
                "rate": targets.get("rate_target_a_per_s"),
                "band": targets.get("pid_temp_band_c"),
            }
            tolerances = {"temp": 0.051, "rate": 0.00051, "band": 0.0051}
            try:
                acknowledged = all(
                    live_values[key] is not None
                    and abs(float(live_values[key]) - float(pending[key])) <= tolerances[key]
                    for key in pending
                )
            except Exception:
                acknowledged = False
            if not acknowledged:
                return

            edits = {
                "temp": self.target_temp,
                "rate": self.target_rate,
                "band": self.pid_band,
            }
            labels = {
                "temp": "Temperature target",
                "rate": "Rate target",
                "band": "PID band",
            }
            for key, submitted in pending.items():
                # Clear dirty state only if the operator has not typed a newer value
                # while the submitted command was travelling through the worker.
                try:
                    current = parse_locale_flexible_float(edits[key].text(), labels[key])
                except Exception:
                    continue
                if abs(float(current) - float(submitted)) <= tolerances[key]:
                    self._target_edit_dirty[key] = False
            self._pending_target_values = None
            self._refresh_target_apply_button()

        def _set_target_if_clean(
            self, field_key: str, edit, value: Any, decimals: int
        ) -> None:
            if self._target_edit_dirty.get(field_key, False):
                return
            self._set_if_idle(edit, value, decimals)

        @staticmethod
        def _set_if_idle(edit, value: Any, decimals: int) -> None:
            if value is None or edit.hasFocus():
                return
            try:
                text = f"{float(value):.{decimals}f}"
            except Exception:
                return
            if edit.text() != text:
                edit.setText(text)

        def _runtime_health_tick(self) -> None:
            if spec.stop_event.is_set():
                self._closing_from_stop = True
                self._shutdown_runtime()
                self.close()
                app = QtWidgets.QApplication.instance()
                if app is not None:
                    app.quit()
                return

            now = time.monotonic()
            if self._last_frame_monotonic:
                age_s = max(0.0, now - self._last_frame_monotonic)
                telemetry_state = (
                    "STALE" if age_s > float(spec.telemetry_stale_after_s) else "online"
                )
            else:
                age_s = math.inf
                telemetry_state = "starting"

            age_text = "--" if not math.isfinite(age_s) else f"{age_s * 1000:.0f} ms"
            queue_depth = self._command_thread.pending_count()
            text = (
                f"Telemetry: {telemetry_state} · age {age_text}\n"
                f"Poll {self._last_poll_duration_s * 1000:.1f} ms · "
                f"paint {self._last_render_duration_s * 1000:.1f} ms · "
                f"skipped {self._dropped_frames}\n"
                f"Commands: {self._command_state} · queue {queue_depth}"
            )
            if text != self._last_runtime_health_text:
                self.runtime_health_label.setText(text)
                self._last_runtime_health_text = text
            degraded = bool(self._last_status.get("telemetry_errors"))
            if telemetry_state == "STALE" or degraded:
                badge_text = "DEGRADED" if degraded else "STALE"
                self.telemetry_badge.setText(badge_text)
                self.telemetry_badge.setStyleSheet(
                    "background:#fee2e2; color:#991b1b; border:1px solid #fecaca; "
                    "border-radius:9px; padding:5px 8px; font-size:10px; font-weight:700;"
                )
                self.runtime_health_label.setStyleSheet(
                    "font-family:Consolas, 'Courier New', monospace; color:#b91c1c; font-weight:700;"
                )
            elif telemetry_state == "online":
                self.telemetry_badge.setText("LIVE")
                self.telemetry_badge.setStyleSheet(
                    "background:#dcfce7; color:#166534; border:1px solid #bbf7d0; "
                    "border-radius:9px; padding:5px 8px; font-size:10px; font-weight:700;"
                )
                self.runtime_health_label.setStyleSheet(
                    "font-family:Consolas, 'Courier New', monospace; color:#64748b;"
                )
            else:
                self.telemetry_badge.setText("CONNECTING")
                self.telemetry_badge.setStyleSheet(
                    "background:#e2e8f0; color:#475569; border:1px solid #cbd5e1; "
                    "border-radius:9px; padding:5px 8px; font-size:10px; font-weight:700;"
                )
                self.runtime_health_label.setStyleSheet(
                    "font-family:Consolas, 'Courier New', monospace; color:#64748b;"
                )

        def _shutdown_runtime(self) -> None:
            if self._runtime_shutdown:
                return
            self._runtime_shutdown = True
            self._render_timer.stop()
            self._health_timer.stop()

            self._telemetry_thread.request_shutdown()
            self._telemetry_thread.wait(2000)

            self._command_thread.request_shutdown()
            if not self._command_thread.wait(5000):
                # Never force-terminate a thread that may own a serial/hardware
                # lock. Keep processing events until the established command
                # returns, preserving safe deterministic cleanup.
                while self._command_thread.isRunning():
                    QtWidgets.QApplication.processEvents(
                        QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents,
                        50,
                    )
                    self._command_thread.wait(50)

        def closeEvent(self, event):  # noqa: N802 - Qt API name
            if not self._closing_from_stop and not spec.stop_event.is_set():
                # Window close is a safe-stop request. The window stays open
                # until the established phase abort path completes. Each phase
                # owns its hardware-safe semantics; the shared dashboard never
                # invents a second shutdown sequence or force-closes the process.
                if not self._abort_dispatched:
                    self._abort_dispatched = True
                    self.last_action_label.setText(
                        "Window close requested. Completing the phase safe-stop sequence…"
                    )
                    self.last_action_label.setStyleSheet(
                        "color:#b91c1c; font-weight:700;"
                    )
                    accepted = self._dispatch(
                        "Abort / safe stop",
                        spec.abort,
                        priority=0,
                        allow_duplicate=False,
                    )
                    if not accepted:
                        self._abort_dispatched = False
                event.ignore()
                return

            self._shutdown_runtime()
            event.accept()

    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("NPG Chamber Controller")
    app.setStyle("Fusion")

    window = DashboardWindow()
    window.showMaximized()
    result = int(app.exec())
    if owns_app:
        del app
    return result
