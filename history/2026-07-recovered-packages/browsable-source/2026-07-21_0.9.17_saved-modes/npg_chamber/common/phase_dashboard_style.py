"""Shared visual helpers for the Phase 01 and Phase 03 Matplotlib dashboards.

This module contains presentation-only helpers. It does not read hardware,
change experimental parameters, or participate in control and safety logic.
"""

from __future__ import annotations

from matplotlib.patches import FancyBboxPatch


AXIS_ACCENTS = {
    "ck1": "#15803d",
    "sample": "#0f766e",
    "pressure": "#1d4ed8",
    "temperature": "#7e22ce",
    "current": "#b45309",
    "voltage": "#854d0e",
    "ck1_temperature": "#b91c1c",
}


def style_measurement_axis(ax, title: str, ylabel: str, accent: str) -> None:
    """Apply the common Phase 01/03/04 plot appearance to one axis."""

    ax.set_title(title, fontsize=11.2, fontweight="bold", color=accent, pad=8)
    ax.set_xlabel("Time", fontsize=9.2, color="#475569")
    ax.set_ylabel(ylabel, fontsize=9.2, color="#334155")
    ax.tick_params(axis="x", rotation=25, labelsize=8.4, colors="#475569", pad=2)
    ax.tick_params(axis="y", labelsize=8.4, colors="#475569")
    ax.grid(True, alpha=0.22, color="#9fb3c8", linewidth=0.8)
    ax.set_facecolor("white")
    ax.margins(x=0.02)
    for spine in ax.spines.values():
        spine.set_color("#d6dee8")
        spine.set_linewidth(1.1)


def add_panel_card(
    panel_ax,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    facecolor: str,
    edgecolor: str,
    radius: float = 0.025,
):
    """Add a rounded pastel information card in panel-axis coordinates."""

    patch = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle=f"round,pad=0.012,rounding_size={radius}",
        transform=panel_ax.transAxes,
        facecolor=facecolor,
        edgecolor=edgecolor,
        linewidth=1.0,
        zorder=0.5,
        clip_on=True,
    )
    panel_ax.add_patch(patch)
    return patch


def create_phase_badge(fig, initial_text: str):
    """Create the centered rounded phase title used by the modern dashboards."""

    return fig.text(
        0.405,
        0.958,
        initial_text,
        ha="center",
        va="center",
        fontsize=18,
        fontweight="bold",
        color="white",
        bbox=dict(
            boxstyle="round,pad=0.42",
            facecolor="#475569",
            edgecolor="#475569",
            alpha=0.98,
        ),
    )


def _phase_badge_color(text: str) -> str:
    lowered = str(text).lower()
    if any(token in lowered for token in ("abort", "safety", "error", "stop")):
        return "#b91c1c"
    if any(token in lowered for token in ("finished", "complete", "done")):
        return "#15803d"
    if any(token in lowered for token in ("ramp down", "ramp-down", "close shutter")):
        return "#c2410c"
    if any(token in lowered for token in ("calibration", "evaporation")):
        return "#7e22ce"
    if any(token in lowered for token in ("wait", "shutter")):
        return "#1d4ed8"
    if any(token in lowered for token in ("heat", "heating")):
        return "#0f766e"
    return "#475569"


def update_phase_badge(text_artist, text: str) -> None:
    """Update badge wording and color without creating new figure artists."""

    text_artist.set_text(text)
    color = _phase_badge_color(text)
    bbox = text_artist.get_bbox_patch()
    if bbox is not None:
        bbox.set_facecolor(color)
        bbox.set_edgecolor(color)
