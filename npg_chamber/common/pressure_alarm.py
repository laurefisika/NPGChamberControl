"""System-wide desktop alarm for dangerous chamber-pressure readings.

The laboratory workstation is Windows-based.  On Windows the alarm uses a
native, topmost/system-modal message box so it appears above whichever program
the operator is using.  A repeating critical sound continues while the latest
pressure remains above the configured threshold.

This is a supervisory software alarm.  It does not replace the chamber's
hardware interlocks, gauge controller protections, risk assessment or SOP.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Optional

PopupFunction = Callable[[str, str], None]
BeepFunction = Callable[[], None]


class PressureEmergencyAlarm:
    """Raise one attention-grabbing desktop alarm per pressure excursion.

    The popup is re-shown periodically if the operator dismisses it while the
    pressure remains high.  Hysteresis prevents the alarm from rapidly toggling
    when the reading sits directly on the threshold.
    """

    def __init__(
        self,
        *,
        threshold_mbar: float = 5.0e-6,
        clear_threshold_mbar: Optional[float] = None,
        repeat_popup_s: float = 30.0,
        beep_period_s: float = 1.0,
        title: str = "EMERGENCY: PRESSURE TOO HIGH",
        context: str = "Synthesis chamber",
        popup_function: Optional[PopupFunction] = None,
        beep_function: Optional[BeepFunction] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        threshold_mbar = float(threshold_mbar)
        if not math.isfinite(threshold_mbar) or threshold_mbar <= 0.0:
            raise ValueError("threshold_mbar must be a positive finite number")

        if clear_threshold_mbar is None:
            clear_threshold_mbar = threshold_mbar * 0.90
        clear_threshold_mbar = float(clear_threshold_mbar)
        if not math.isfinite(clear_threshold_mbar) or clear_threshold_mbar < 0.0:
            raise ValueError("clear_threshold_mbar must be finite and >= 0")
        if clear_threshold_mbar >= threshold_mbar:
            raise ValueError("clear_threshold_mbar must be below threshold_mbar")

        self.threshold_mbar = threshold_mbar
        self.clear_threshold_mbar = clear_threshold_mbar
        self.repeat_popup_s = max(1.0, float(repeat_popup_s))
        self.beep_period_s = max(0.25, float(beep_period_s))
        self.title = str(title)
        self.context = str(context)
        self._popup_function = popup_function or _default_popup
        self._beep_function = beep_function or _default_critical_beep
        self._clock = clock

        self._lock = threading.RLock()
        self._active = False
        self._closed = False
        self._popup_open = False
        self._last_popup_at = float("-inf")
        self._last_pressure_mbar: Optional[float] = None
        self._sound_stop = threading.Event()
        self._sound_thread: Optional[threading.Thread] = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def update(
        self,
        pressure_mbar: Optional[float],
        *,
        enabled: bool = True,
        context: Optional[str] = None,
    ) -> bool:
        """Process one pressure sample and return whether alarm state is active."""

        if not enabled:
            self.clear()
            return False

        try:
            pressure = float(pressure_mbar)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return self.active
        if not math.isfinite(pressure):
            return self.active

        popup_context = self.context if context is None else str(context)
        now = self._clock()
        should_start_sound = False
        should_show_popup = False

        with self._lock:
            if self._closed:
                return False

            self._last_pressure_mbar = pressure
            if pressure > self.threshold_mbar:
                if not self._active:
                    self._active = True
                    self._sound_stop.clear()
                    should_start_sound = True
                    should_show_popup = True
                elif (
                    not self._popup_open
                    and now - self._last_popup_at >= self.repeat_popup_s
                ):
                    should_show_popup = True
            elif self._active and pressure <= self.clear_threshold_mbar:
                self._deactivate_locked()
                return False

        if should_start_sound:
            self._start_sound_thread()
        if should_show_popup:
            self._start_popup_thread(pressure, popup_context)
        return self.active

    def clear(self) -> None:
        """Clear the alarm and stop its repeating sound."""

        with self._lock:
            self._deactivate_locked()

    def close(self) -> None:
        """Permanently stop this alarm instance during process shutdown."""

        with self._lock:
            self._closed = True
            self._deactivate_locked()

    def _deactivate_locked(self) -> None:
        self._active = False
        self._last_pressure_mbar = None
        self._sound_stop.set()

    def _start_sound_thread(self) -> None:
        with self._lock:
            if self._sound_thread is not None and self._sound_thread.is_alive():
                return
            self._sound_thread = threading.Thread(
                target=self._sound_loop,
                name="pressure-emergency-sound",
                daemon=True,
            )
            self._sound_thread.start()

    def _sound_loop(self) -> None:
        while not self._sound_stop.is_set():
            try:
                self._beep_function()
            except Exception as exc:
                print(f"Pressure alarm sound failed: {exc}", file=sys.stderr)
            self._sound_stop.wait(self.beep_period_s)

    def _start_popup_thread(self, pressure_mbar: float, context: str) -> None:
        with self._lock:
            if self._closed or self._popup_open:
                return
            self._popup_open = True
            self._last_popup_at = self._clock()

        message = (
            "EMERGENCY: PRESSURE TOO HIGH\n\n"
            f"{context}\n"
            f"Measured pressure: {pressure_mbar:.2e} mbar\n"
            f"Alarm threshold: {self.threshold_mbar:.2e} mbar\n\n"
            "Check the chamber immediately and follow the emergency procedure."
        )

        thread = threading.Thread(
            target=self._popup_worker,
            args=(message,),
            name="pressure-emergency-popup",
            daemon=True,
        )
        thread.start()

    def _popup_worker(self, message: str) -> None:
        try:
            self._popup_function(self.title, message)
        except Exception as exc:
            print(f"{self.title}: {message}\nPopup failed: {exc}", file=sys.stderr)
        finally:
            with self._lock:
                self._popup_open = False
                self._last_popup_at = self._clock()


def _default_critical_beep() -> None:
    if os.name == "nt":
        import winsound

        winsound.MessageBeep(winsound.MB_ICONHAND)
        return

    # Development/test fallback.  The real chamber workstation uses Windows.
    try:
        print("\a", end="", flush=True, file=sys.stderr)
    except Exception:
        pass


def _default_popup(title: str, message: str) -> None:
    if os.name == "nt":
        import ctypes

        mb_ok = 0x00000000
        mb_iconerror = 0x00000010
        mb_setforeground = 0x00010000
        mb_systemmodal = 0x00001000
        mb_topmost = 0x00040000
        flags = mb_ok | mb_iconerror | mb_setforeground | mb_systemmodal | mb_topmost
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        return

    # Cross-platform fallback for development.  It is intentionally best-effort
    # because headless CI systems may not have a graphical display.
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update_idletasks()
        messagebox.showerror(title, message, parent=root)
        root.destroy()
    except Exception:
        print(f"{title}\n{message}", file=sys.stderr)
