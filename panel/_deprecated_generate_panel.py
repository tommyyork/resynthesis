#!/usr/bin/env python3
"""
DEPRECATED: Legacy Resynthesis front-panel SVG generator.

This module is deprecated. Prefer `generate_panel_kicad.py` for panel generation.
It is kept for backward compatibility and tests that still import panel constants
and builders (e.g. build_panel_svg, build_background_svg, build_silkscreen_svg).

Originally generated the canonical Resynthesis front-panel SVG (ResynthesisPanel.svg):
3U × 10HP (128.5 × 50.8 mm) with drill centres, SD-card cutout, and Eurorack
mounting screw slots matching the Patch.Init KiCad PCB and eurorack_spec.

To regenerate the panel SVG using this deprecated script:

  python3 _deprecated_generate_panel.py
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from string import Template


from generate_panel_kicad import (
    DEFAULT_KICAD_PCB,
    FAMILY_JACK,
    FAMILY_LED,
    FAMILY_POT,
    FAMILY_SWITCH_B7,
    FAMILY_SWITCH_B8,
    _parse_holes_from_kicad,
    _parse_sd_slot_from_kicad,
)

HERE = Path(__file__).parent
OUTPUT_DIR = HERE / "output"
DEFAULT_OUTPUT = OUTPUT_DIR / "ResynthesisPanel.svg"
SILKSCREEN_OUTPUT = OUTPUT_DIR / "silkscreen.svg"
BACKGROUND_OUTPUT = OUTPUT_DIR / "background.svg"

# 3U × 10HP panel geometry (mm), matching eurorack_spec/README.md.
PANEL_WIDTH_MM = 50.8
PANEL_HEIGHT_MM = 128.5

# Drill diameters (mm) from Patch.Init KiCad / panel datasheets.


def _npth_family_diameters() -> dict[str, float]:
    """Per-family drill diameters (mm) from Patch.Init / panel datasheets."""
    return {
        "mount": 3.0,
        "led": 3.2,
        "switch_b7": 5.5,
        "jack": 6.2,
        "pot": 7.2,
    }


_NPTH_FAMILY_DIAMETERS_MM = _npth_family_diameters()

# Per-family NPTH drill diameters (mm), derived from blank-NPTH.drl.
MOUNT_DRILL_DIAMETER_MM = _NPTH_FAMILY_DIAMETERS_MM["mount"]
LED_DRILL_DIAMETER_MM = _NPTH_FAMILY_DIAMETERS_MM["led"]
SWITCH_B7_DRILL_DIAMETER_MM = _NPTH_FAMILY_DIAMETERS_MM["switch_b7"]
JACK_DRILL_DIAMETER_MM = _NPTH_FAMILY_DIAMETERS_MM["jack"]
POT_DRILL_DIAMETER_MM = _NPTH_FAMILY_DIAMETERS_MM["pot"]

# B_8 toggle uses the jack-family 6.2 mm drill tool.
SWITCH_B8_DRILL_DIAMETER_MM = JACK_DRILL_DIAMETER_MM

# Component-family **panel hole** diameters (mm).
#
# These are the final cut-out sizes used in the SVG. They are derived from the
# NPTH drill diameters above, with a small clearance margin so that hardware
# drops cleanly into the panel while keeping the drill files as the mechanical
# source of truth.
#
# Potentiometers currently use a slightly larger clearance than jacks to match
# the existing checked-in artwork.
POT_PANEL_DIAMETER_MM = POT_DRILL_DIAMETER_MM + 0.3
SWITCH_B7_PANEL_DIAMETER_MM = SWITCH_B7_DRILL_DIAMETER_MM + 0.0
SWITCH_B8_PANEL_DIAMETER_MM = SWITCH_B8_DRILL_DIAMETER_MM + 0.1
JACK_PANEL_DIAMETER_MM = JACK_DRILL_DIAMETER_MM + 0.1


def _format_screw_slots() -> str:
    """
    Return four wide, black-filled rectangular screw slots as SVG <rect> lines.

    Requirements and assumptions:
    - Mounting rows are 3 mm from the top and bottom edges (Eurorack standard),
      so the slot centres lie at y = 3 mm and y = 128.5 − 3 = 125.5 mm.
    - The left/right X positions for two of the slots are aligned with the
      Patch.Init NPTH 3.0 mm mounting holes, whose panel-local centres are
      (7.50, 3.00) and (43.10, 125.50) mm in the canonical layout.
    - We add the complementary two slots at the remaining corners so the panel
      can be mounted with four screws while still matching the original board
      hardware for the two stock mounting holes.

    Slots are "wide rather than tall": width > height in panel coordinates.
    The tests in `test_panel_alignment.py` treat these as black-filled rects
    with small dimensions in mm, and verify that their vertical centres are
    3 mm from the top/bottom edges (rail alignment).
    """
    screw_width_mm = 5.0
    screw_height_mm = 3.0

    # Canonical mounting row Y positions from Eurorack spec.
    y_top = 3.0
    y_bottom = PANEL_HEIGHT_MM - 3.0

    # X positions: left side matches the existing canonical panel (7.50 mm);
    # right side matches the existing Patch.Init mounting hole centre at 43.10 mm.
    x_left = 7.50
    x_right = 43.10

    centers = [
        (x_left, y_top),
        (x_right, y_top),
        (x_left, y_bottom),
        (x_right, y_bottom),
    ]

    lines: list[str] = []
    for cx, cy in centers:
        x = cx - screw_width_mm / 2.0
        y = cy - screw_height_mm / 2.0
        lines.append(
            (
                f'  <rect x="{x:.3f}" y="{y:.3f}" '
                f'width="{screw_width_mm:.3f}" height="{screw_height_mm:.3f}" '
                f'rx="1.0" fill="#000000" stroke="#ffffff" stroke-width="0.2" />'
            )
        )
    return "\n".join(lines)


# NOTE: This template is intentionally very close to the checked-in
# `ResynthesisPanel.svg`. Only the mounting screw geometry is parameterised
# via $SCREW_SLOTS so that the Eurorack rail alignment and rectangular-slot
# requirement are explicit and testable from code.
PANEL_TEMPLATE = Template(
    """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!--
  Front-panel PCB artwork for the Resynthesis module (Patch.Init format).
  Units: mm. Size: 3U x 10HP (128.5 x 50.8 mm).
  All labels centered on their drill; jack labels above each jack.
  Font: open-source DIN-style (see README). All text all caps.
-->
<svg
  xmlns="http://www.w3.org/2000/svg"
  width="50.8mm"
  height="128.5mm"
  viewBox="0 0 50.8 128.5"
>
  <defs>
    <style type="text/css">
      .panel-text { font-family: Gidole, 'DIN Alternate', 'DIN 2014', sans-serif; }
    </style>
    <!-- Copper-only background patterns:
         - Left: large broken squares
         - Middle: debris/fragmented lines (letters washing off)
         - Right: large broken circles
         All strokes use a single copper colour with no fills or tones. -->

    <!-- Left: large broken squares (rect only, no circles) -->
    <pattern id="patternSquares" width="4" height="4" patternUnits="userSpaceOnUse">
      <rect x="0" y="0" width="4" height="4"
            fill="none"
            stroke="#d4af37"
            stroke-width="0.2"
            stroke-dasharray="2 1" />
    </pattern>

    <!-- Middle: debris / falling fragments -->
    <pattern id="patternDebris" width="4" height="4" patternUnits="userSpaceOnUse">
      <!-- Short, staggered segments suggesting pieces falling off -->
      <line x1="0.5" y1="0.4" x2="2.0" y2="0.4"
            stroke="#d4af37"
            stroke-width="0.18" />
      <line x1="2.2" y1="1.4" x2="3.5" y2="1.4"
            stroke="#d4af37"
            stroke-width="0.18"
            stroke-dasharray="0.6 0.4" />
      <line x1="0.2" y1="2.1" x2="1.4" y2="2.7"
            stroke="#d4af37"
            stroke-width="0.18" />
      <line x1="2.0" y1="2.8" x2="3.8" y2="3.2"
            stroke="#d4af37"
            stroke-width="0.18"
            stroke-dasharray="0.4 0.6" />
    </pattern>

    <!-- Right: circle-like fragments built from short line segments (no <circle> elements) -->
    <pattern id="patternCircles" width="4" height="4" patternUnits="userSpaceOnUse">
      <!-- Four short chords hinting at a circle outline -->
      <line x1="1.0" y1="0.8" x2="2.4" y2="0.6"
            stroke="#d4af37"
            stroke-width="0.2" />
      <line x1="2.6" y1="1.0" x2="3.2" y2="2.0"
            stroke="#d4af37"
            stroke-width="0.2" />
      <line x1="3.0" y1="2.6" x2="1.8" y2="3.2"
            stroke="#d4af37"
            stroke-width="0.2" />
      <line x1="1.0" y1="3.0" x2="0.6" y2="1.8"
            stroke="#d4af37"
            stroke-width="0.2" />
    </pattern>

    <!-- Masks to grade the background from squares (left) through debris (middle) to circles (right).
         Squares are present across the full panel; debris and circle fragments become denser toward the right. -->
    <mask id="maskLeft">
      <!-- Squares everywhere -->
      <rect x="0" y="0" width="50.8" height="128.5" fill="white" />
    </mask>
    <mask id="maskMid">
      <!-- Debris mainly in the centre, with a soft-edged, irregular band -->
      <rect x="10" y="0" width="14" height="128.5" fill="white" />
      <rect x="18" y="0" width="12" height="128.5" fill="white" />
      <rect x="24" y="0" width="10" height="128.5" fill="white" />
    </mask>
    <mask id="maskRight">
      <!-- Circle fragments sparse in the mid-right, dense at the far right -->
      <rect x="26" y="0" width="8" height="128.5" fill="white" />
      <rect x="32" y="0" width="10" height="128.5" fill="white" />
      <rect x="38" y="0" width="12.8" height="128.5" fill="white" />
    </mask>
  </defs>

  <rect x="0" y="0" width="50.8" height="128.5" fill="#050505" />
  <!-- Background transition: squares -> debris -> circles (all rects full-size, sliced by masks) -->
  <rect x="0" y="0" width="50.8" height="128.5" fill="url(#patternSquares)" mask="url(#maskLeft)" />
  <rect x="0" y="0" width="50.8" height="128.5" fill="url(#patternDebris)" mask="url(#maskMid)" />
  <rect x="0" y="0" width="50.8" height="128.5" fill="url(#patternCircles)" mask="url(#maskRight)" />
  <rect x="0.15" y="0.15" width="50.5" height="128.2" fill="none" stroke="#d4af37" stroke-width="0.3" />

  <!-- Title (above all drills) -->
  <text x="25.4" y="8" class="panel-text" font-size="4" text-anchor="middle" fill="#ffffff">&#21270;</text>
  <text x="25.4" y="13" class="panel-text" font-size="3.6" text-anchor="middle" fill="#f5e3a1">RESYNTHESIS</text>

  <!-- Mounting screw slots: wide rectangular, aligned with Eurorack rails (3 mm from top/bottom) -->
$SCREW_SLOTS

  <!-- SD card holder cutout (from Patch.Init KiCad PCB) -->
  <rect x="24.14" y="33.493" width="3.208" height="12.802" fill="none" stroke="#ffffff" stroke-width="0.2" />

  <!-- Pots CV_1-CV_4 (9MM_SNAP-IN_POT… → ${POT_DIAMETER_MM} mm panel holes) -->
  <circle cx="11.176" cy="22.904" r="${POT_R}" fill="none" stroke="#ffffff" stroke-width="0.3" />
  <circle cx="39.65" cy="22.904" r="${POT_R}" fill="none" stroke="#ffffff" stroke-width="0.3" />
  <circle cx="11.176" cy="42.027" r="${POT_R}" fill="none" stroke="#ffffff" stroke-width="0.3" />
  <circle cx="39.65" cy="42.027" r="${POT_R}" fill="none" stroke="#ffffff" stroke-width="0.3" />

  <!-- Labels beneath pots row 1 (y >= 26.5), font 3.6 mm (>= 10 pt); clear of 12mm knob -->
  <text x="11.176" y="33" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">OFFER</text>
  <text x="39.65" y="33" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">TIMESTRETCH</text>

  <!-- Labels beneath pots row 2 (y >= 45.6); clear of knob. CV_3 labeled FLUFF, CV_4 labeled COLOR -->
  <text x="11.176" y="52" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">FLUFF</text>
  <text x="39.65" y="52" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">COLOR</text>

  <!-- T2 LED; T3/T4 jacks and switches -->
  <circle cx="25.4" cy="19.252" r="1.6" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <!-- B_7 (MAX COMP) uses TL1105… footprint → ${SWITCH_B7_DIAMETER_MM} mm panel hole.
       B_8 (PITCH LOCK) uses a toggle on the jack-family 6.2 mm drill → ${SWITCH_B8_DIAMETER_MM} mm panel hole. -->
  <circle cx="8.65" cy="59.288" r="${SWITCH_R_B7}" fill="none" stroke="#ffffff" stroke-width="0.3" />
  <circle cx="25.503" cy="61.957" r="${SWITCH_R_B8}" fill="none" stroke="#ffffff" stroke-width="0.3" />

  <!-- All jacks use S_JACK footprints → ${JACK_DIAMETER_MM} mm panel holes -->
  <circle cx="7.15" cy="84.562" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="7.15" cy="98.312" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="7.15" cy="111.9" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="19.317" cy="84.562" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="19.317" cy="98.312" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="19.317" cy="111.9" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="31.483" cy="84.562" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="31.483" cy="98.312" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="31.483" cy="111.9" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="42.155" cy="59.288" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="43.65" cy="84.562" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="43.65" cy="98.312" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />
  <circle cx="43.65" cy="111.9" r="${JACK_R}" fill="none" stroke="#ffffff" stroke-width="0.2" />

  <!-- Output jack grouping: individual rounded rectangles centered on each jack. -->
  <!-- CV_OUT_1 at (42.155, 59.288) -->
  <rect x="38.155" y="55.288" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />
  <!-- B5 at (31.483, 84.562) -->
  <rect x="27.483" y="80.562" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />
  <!-- B6 at (43.65, 84.562) -->
  <rect x="39.650" y="80.562" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />
  <!-- OUT L at (31.483, 111.9) -->
  <rect x="27.483" y="107.900" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />
  <!-- OUT R at (43.65, 111.9) -->
  <rect x="39.650" y="107.900" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />

  <!-- Left switch (B_7, MAX COMP) at (8.65, 59.288): panel label split over two lines -->
  <text x="8.65" y="66.5" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">MAX</text>
  <text x="8.65" y="70.03" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">COMP</text>

  <!-- Centre switch (B_8, mode) at (25.503, 61.957): panel label PITCH LOCK, split over two lines.
       The top line is moved down so that the minimum vertical clearance from the drill center
       matches the padding used for THOUGHTS and MAX COMP. -->
  <text x="25.503" y="69.2" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">PITCH</text>
  <text x="25.503" y="72.73" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">LOCK</text>

  <!-- CV_OUT_1 / C10 jack at (42.155, 59.288): panel label THOUGHTS (rendered as !!! on panel) -->
  <text x="42.155" y="66.9" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">!!!</text>

  <!-- 12 jacks: labels OVER each jack, centered. Top row (y=84.562) ? B10, B9, B5, B6 -->
  <text x="7.15" y="79.0" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">B10</text>
  <text x="19.317" y="79.0" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">B9</text>
  <text x="31.483" y="79.0" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">B5</text>
  <text x="43.65" y="79.0" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">B6</text>

  <!-- Middle row (y=98.312) – V/OCT, SMOOTH, SPARSITY, italic d (diffusion) -->
  <text x="7.15" y="92.8" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">V/OCT</text>
  <text x="19.317" y="92.8" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">SMOOTH</text>
  <text x="31.483" y="92.8" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">SPARSITY</text>
  <text x="43.65" y="92.8" class="panel-text" font-size="3.6" text-anchor="middle"
        fill="#ffffff"
        font-family="DIN 2014, Gidole, 'DIN Alternate', sans-serif"
        font-style="italic">D</text>

  <!-- Bottom row (y=111.9) ? IN L, IN R, OUT L, OUT R -->
  <text x="7.15" y="106.4" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">IN L</text>
  <text x="19.317" y="106.4" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">IN R</text>
  <text x="31.483" y="106.4" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">OUT L</text>
  <text x="43.65" y="106.4" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">OUT R</text>

</svg>
"""
)

# Same dimensions as hardware-centers-kicad.svg (50.8 × 128.5 mm).
SVG_VIEWBOX = "0 0 50.8 128.5"
SVG_DIMS = 'width="50.8mm" height="128.5mm" viewBox="0 0 50.8 128.5"'

# Background-only SVG: defs (patterns + masks), base rect, pattern rects, gold border. No holes, text, or jack boxes.
BACKGROUND_TEMPLATE = Template(
    """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!-- Background layer for Resynthesis panel. Same dimensions as hardware-centers-kicad.svg. -->
<svg
  xmlns="http://www.w3.org/2000/svg"
  $DIMS
>
  <defs>
    <style type="text/css">
      .panel-text { font-family: Gidole, 'DIN Alternate', 'DIN 2014', sans-serif; }
    </style>
    <pattern id="patternSquares" width="4" height="4" patternUnits="userSpaceOnUse">
      <rect x="0" y="0" width="4" height="4"
            fill="none"
            stroke="#d4af37"
            stroke-width="0.2"
            stroke-dasharray="2 1" />
    </pattern>
    <pattern id="patternDebris" width="4" height="4" patternUnits="userSpaceOnUse">
      <line x1="0.5" y1="0.4" x2="2.0" y2="0.4"
            stroke="#d4af37"
            stroke-width="0.18" />
      <line x1="2.2" y1="1.4" x2="3.5" y2="1.4"
            stroke="#d4af37"
            stroke-width="0.18"
            stroke-dasharray="0.6 0.4" />
      <line x1="0.2" y1="2.1" x2="1.4" y2="2.7"
            stroke="#d4af37"
            stroke-width="0.18" />
      <line x1="2.0" y1="2.8" x2="3.8" y2="3.2"
            stroke="#d4af37"
            stroke-width="0.18"
            stroke-dasharray="0.4 0.6" />
    </pattern>
    <pattern id="patternCircles" width="4" height="4" patternUnits="userSpaceOnUse">
      <line x1="1.0" y1="0.8" x2="2.4" y2="0.6"
            stroke="#d4af37"
            stroke-width="0.2" />
      <line x1="2.6" y1="1.0" x2="3.2" y2="2.0"
            stroke="#d4af37"
            stroke-width="0.2" />
      <line x1="3.0" y1="2.6" x2="1.8" y2="3.2"
            stroke="#d4af37"
            stroke-width="0.2" />
      <line x1="1.0" y1="3.0" x2="0.6" y2="1.8"
            stroke="#d4af37"
            stroke-width="0.2" />
    </pattern>
    <mask id="maskLeft">
      <rect x="0" y="0" width="50.8" height="128.5" fill="white" />
    </mask>
    <mask id="maskMid">
      <rect x="10" y="0" width="14" height="128.5" fill="white" />
      <rect x="18" y="0" width="12" height="128.5" fill="white" />
      <rect x="24" y="0" width="10" height="128.5" fill="white" />
    </mask>
    <mask id="maskRight">
      <rect x="26" y="0" width="8" height="128.5" fill="white" />
      <rect x="32" y="0" width="10" height="128.5" fill="white" />
      <rect x="38" y="0" width="12.8" height="128.5" fill="white" />
    </mask>
  </defs>

  <rect x="0" y="0" width="50.8" height="128.5" fill="#050505" />
  <rect x="0" y="0" width="50.8" height="128.5" fill="url(#patternSquares)" mask="url(#maskLeft)" />
  <rect x="0" y="0" width="50.8" height="128.5" fill="url(#patternDebris)" mask="url(#maskMid)" />
  <rect x="0" y="0" width="50.8" height="128.5" fill="url(#patternCircles)" mask="url(#maskRight)" />
  <rect x="0.15" y="0.15" width="50.5" height="128.2" fill="none" stroke="#d4af37" stroke-width="0.3" />
</svg>
"""
)

# Silkscreen layer: all text labels + five white rounded rectangles around the output jacks. Same dimensions.
SILKSCREEN_TEMPLATE = Template(
    """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!-- Silkscreen layer: text labels and five white jack boxes. Same dimensions as hardware-centers-kicad.svg. -->
<svg
  xmlns="http://www.w3.org/2000/svg"
  $DIMS
>
  <defs>
    <style type="text/css">
      .panel-text { font-family: Gidole, 'DIN Alternate', 'DIN 2014', sans-serif; }
    </style>
  </defs>

  <!-- Five white rectangular boxes around the jacks (CV_OUT_1, B5, B6, OUT L, OUT R) -->
  <rect x="38.155" y="55.288" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />
  <rect x="27.483" y="80.562" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />
  <rect x="39.650" y="80.562" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />
  <rect x="27.483" y="107.900" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />
  <rect x="39.650" y="107.900" width="8.0" height="8.0" rx="1.2"
        fill="#ffffff" stroke="#ffffff" stroke-width="0.2" />

  <!-- Title -->
  <text x="25.4" y="8" class="panel-text" font-size="4" text-anchor="middle" fill="#ffffff">&#21270;</text>
  <text x="25.4" y="13" class="panel-text" font-size="3.6" text-anchor="middle" fill="#f5e3a1">RESYNTHESIS</text>

  <!-- Pot labels -->
  <text x="11.176" y="33" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">OFFER</text>
  <text x="39.65" y="33" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">TIMESTRETCH</text>
  <text x="11.176" y="52" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">FLUFF</text>
  <text x="39.65" y="52" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">COLOR</text>

  <!-- Switch labels -->
  <text x="8.65" y="66.5" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">MAX</text>
  <text x="8.65" y="70.03" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">COMP</text>
  <text x="25.503" y="69.2" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">PITCH</text>
  <text x="25.503" y="72.73" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">LOCK</text>
  <text x="42.155" y="66.9" class="panel-text" font-size="3.53" text-anchor="middle" fill="#ffffff">!!!</text>

  <!-- Jack labels: top row -->
  <text x="7.15" y="79.0" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">B10</text>
  <text x="19.317" y="79.0" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">B9</text>
  <text x="31.483" y="79.0" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">B5</text>
  <text x="43.65" y="79.0" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">B6</text>

  <!-- Jack labels: middle row -->
  <text x="7.15" y="92.8" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">V/OCT</text>
  <text x="19.317" y="92.8" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">SMOOTH</text>
  <text x="31.483" y="92.8" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">SPARSITY</text>
  <text x="43.65" y="92.8" class="panel-text" font-size="3.6" text-anchor="middle"
        fill="#ffffff"
        font-family="DIN 2014, Gidole, 'DIN Alternate', sans-serif"
        font-style="italic">D</text>

  <!-- Jack labels: bottom row -->
  <text x="7.15" y="106.4" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">IN L</text>
  <text x="19.317" y="106.4" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">IN R</text>
  <text x="31.483" y="106.4" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">OUT L</text>
  <text x="43.65" y="106.4" class="panel-text" font-size="3.6" text-anchor="middle" fill="#ffffff">OUT R</text>
</svg>
"""
)


def _load_drill_holes_from_kicad(kicad_path: Path | None = None) -> list[tuple[float, float, float]]:
    """Load NPTH hole centres from Patch.Init KiCad PCB as panel-local (x, y, r) mm.
    r is the panel cutout radius (for drawing).
    """
    path = kicad_path or DEFAULT_KICAD_PCB
    if not path.exists():
        return []
    raw = _parse_holes_from_kicad(path)  # (x, y, family)
    r_by_family = {
        FAMILY_POT: POT_PANEL_DIAMETER_MM / 2.0,
        FAMILY_JACK: JACK_PANEL_DIAMETER_MM / 2.0,
        FAMILY_SWITCH_B7: SWITCH_B7_PANEL_DIAMETER_MM / 2.0,
        FAMILY_SWITCH_B8: SWITCH_B8_PANEL_DIAMETER_MM / 2.0,
        FAMILY_LED: LED_DRILL_DIAMETER_MM / 2.0,
    }
    return [(x, y, r_by_family.get(f, 0.7)) for (x, y, f) in raw]


def _load_sd_slot_from_kicad(kicad_path: Path | None = None) -> tuple[float, float, float, float] | None:
    """Load SD card cutout from Patch.Init KiCad PCB as panel-local (x, y, w, h) mm."""
    path = kicad_path or DEFAULT_KICAD_PCB
    if not path.exists():
        return None
    return _parse_sd_slot_from_kicad(path)


def _add_text_backgrounds(svg: str) -> str:
    """Add rounded black background rectangles behind all <text> elements.

    The rectangles are sized using the same approximate text metrics as
    test_panel_alignment._approx_text_bbox so that the visual boxes closely
    follow the rendered text while remaining purely decorative (non‑mechanical).
    """
    import re as re_mod

    pattern = re_mod.compile(r"(<text\b[^>]*>)(.*?)(</text>)", re_mod.DOTALL)

    def _repl(match: re_mod.Match[str]) -> str:
        open_tag, inner, close_tag = match.group(1), match.group(2), match.group(3)

        x_m = re_mod.search(r'x="([^"]+)"', open_tag)
        y_m = re_mod.search(r'y="([^"]+)"', open_tag)
        fs_m = re_mod.search(r'font-size="([^"]+)"', open_tag)
        if not (x_m and y_m and fs_m):
            return match.group(0)

        try:
            x = float(eval(x_m.group(1), {}, {}))
            y = float(eval(y_m.group(1), {}, {}))
            fs = float(eval(fs_m.group(1), {}, {}))
        except Exception:
            return match.group(0)

        anchor_m = re_mod.search(r'text-anchor="([^"]+)"', open_tag)
        anchor = anchor_m.group(1) if anchor_m else "start"

        # Strip any nested tags and normalise whitespace inside the text node.
        text_content = re_mod.sub(r"<.*?>", "", inner)
        text_content = " ".join(text_content.split())
        if not text_content:
            return match.group(0)

        # Approximate bbox using the same heuristics as _approx_text_bbox.
        char_w = 0.60 * fs
        width = char_w * len(text_content)
        height = 1.0 * fs

        if anchor == "middle":
            minx = x - width / 2.0
            maxx = x + width / 2.0
        elif anchor == "end":
            minx = x - width
            maxx = x
        else:
            minx = x
            maxx = x + width

        miny = y - 0.80 * height
        maxy = y + 0.20 * height

        pad = 0.6  # mm
        rect_x = minx - pad
        rect_y = miny - pad
        rect_w = (maxx - minx) + 2.0 * pad
        rect_h = (maxy - miny) + 2.0 * pad
        rx = 0.4 * fs

        rect = (
            f'<rect x="{rect_x:.3f}" y="{rect_y:.3f}" '
            f'width="{rect_w:.3f}" height="{rect_h:.3f}" '
            f'rx="{rx:.3f}" fill="#000000" stroke="none" '
            f'data-panel-role="label-bg" />'
        )
        return rect + "\n  " + match.group(0)

    return pattern.sub(_repl, svg)


def build_background_svg() -> str:
    """Build background-only SVG (same dimensions as hardware-centers-kicad.svg)."""
    return BACKGROUND_TEMPLATE.substitute(DIMS=SVG_DIMS)


def build_silkscreen_svg() -> str:
    """Build silkscreen SVG: text labels + five white jack boxes (same dimensions)."""
    return SILKSCREEN_TEMPLATE.substitute(DIMS=SVG_DIMS)


def build_panel_svg() -> str:
    """Render the full panel SVG as a string."""
    screw_slots = _format_screw_slots()
    pot_r = POT_PANEL_DIAMETER_MM / 2.0
    switch_b7_r = SWITCH_B7_PANEL_DIAMETER_MM / 2.0
    jack_r = JACK_PANEL_DIAMETER_MM / 2.0

    # PITCH LOCK / B_8 uses the jack-family 6.2 mm NPTH drill (T4) for its
    # panel cutout. Enlarge the decorative white outline so it fully encircles
    # the 6.2 mm hole and remains visible outside the solid black NPTH overlay.
    switch_b8_r = JACK_PANEL_DIAMETER_MM / 2.0 + 0.25

    svg = PANEL_TEMPLATE.substitute(
        SCREW_SLOTS=screw_slots,
        POT_R=f"{pot_r:.3f}",
        SWITCH_R_B7=f"{switch_b7_r:.3f}",
        SWITCH_R_B8=f"{switch_b8_r:.3f}",
        JACK_R=f"{jack_r:.3f}",
        POT_DIAMETER_MM=f"{POT_PANEL_DIAMETER_MM:.1f}",
        SWITCH_B7_DIAMETER_MM=f"{SWITCH_B7_PANEL_DIAMETER_MM:.1f}",
        SWITCH_B8_DIAMETER_MM=f"{SWITCH_B8_PANEL_DIAMETER_MM:.1f}",
        JACK_DIAMETER_MM=f"{JACK_PANEL_DIAMETER_MM:.1f}",
    )

    # Ensure all circular drill holes from the panel artwork render as solid
    # black rather than letting the background pattern show through.
    svg = re.sub(
        r'(<circle\b[^>]*?)\s+fill="none"([^>]*?stroke="#ffffff"[^>]*?/>)',
        r'\1 fill="#000000"\2',
        svg,
    )

    # Add black, rounded backgrounds behind all text labels so that each legend
    # is rendered on a clear, legible box above the patterned copper background.
    svg = _add_text_backgrounds(svg)

    # Additionally, overlay solid black geometry for all mechanical cutouts
    # derived from the Patch.Init manufacturing files:
    # - Circular NPTH drill holes from blank-NPTH.drl.
    # - Rectangular SD card holder slot from blank-Edge_Cuts.gbr.
    # This guarantees that all PCB drill/cut regions are represented as solid
    # black areas in the SVG, even if the template artwork is edited.
    overlay_blocks: list[str] = []

    holes = _load_drill_holes_from_kicad()
    if holes:
        npth_lines = [
            '  <!-- NPTH drill layer overlay: solid black holes -->',
            '  <g id="npth_drills" fill="#000000" stroke="none">',
        ]
        for (x, y, r) in holes:
            npth_lines.append(f'    <circle cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" />')
        npth_lines.append("  </g>")
        overlay_blocks.append("\n".join(npth_lines))

    sd_slot = _load_sd_slot_from_kicad()
    if sd_slot is not None:
        sx, sy, sw, sh = sd_slot
        sd_lines = [
            "  <!-- SD card holder cutout overlay: solid black slot from Edge_Cuts -->",
            '  <g id="sd_slot" fill="#000000" stroke="none">',
            f'    <rect x="{sx:.3f}" y="{sy:.3f}" width="{sw:.3f}" height="{sh:.3f}" />',
            "  </g>",
        ]
        overlay_blocks.append("\n".join(sd_lines))

    if overlay_blocks:
        overlay = "\n".join(overlay_blocks) + "\n"
        svg = svg.replace("</svg>\n", overlay + "</svg>\n")

    return svg


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the canonical Resynthesis panel SVG (ResynthesisPanel.svg). [DEPRECATED: prefer generate_panel_kicad.py]"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output SVG path (default: ResynthesisPanel.svg in this directory)",
    )
    args = parser.parse_args()

    svg = build_panel_svg()
    output_path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg, encoding="utf-8")
    print(f"Wrote Resynthesis panel SVG → {output_path}")

    # Same-dimension layers for KiCad / fabrication (50.8 × 128.5 mm).
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    BACKGROUND_OUTPUT.write_text(build_background_svg(), encoding="utf-8")
    print(f"Wrote background layer → {BACKGROUND_OUTPUT}")
    SILKSCREEN_OUTPUT.write_text(build_silkscreen_svg(), encoding="utf-8")
    print(f"Wrote silkscreen layer → {SILKSCREEN_OUTPUT}")


if __name__ == "__main__":
    main()
