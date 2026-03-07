#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate a KiCad PCB for the Resynthesis front panel with the same dimensions
as the patch_init gerbers (50.8 × 128.5 mm). Uses hardware positions from the
Patch.Init KiCad PCB.

Each component type gets a **panel** footprint: the original Patch.Init footprint’s
silkscreen/outline is placed on a drawing layer (Dwgs.User), and the drill is the
**panel cutout** diameter (drill + clearance from datasheets/README.md). These
match the geometry in footprint_calc/*_panel_overlay.svg (black = component
outline, red circle = panel cutout). The generated PCB’s drill and Edge.Cuts
match the calculated cuts in hardware-centers-kicad.svg.

Usage:
  python3 generate_panel_kicad.py [-o output_dir] [kicad_pcb_path]

Output: panel/output/generated_panel/ with:
  - generated_panel.kicad_pcb
  - generated_panel.pretty/ (panel footprints: Panel_9MM_Pot, Panel_Jack, etc.)
  - generated_panel.kicad_pro, fp-lib-table
  - hardware-centers-kicad.svg (reference for validation)
"""

from __future__ import annotations

import argparse
import math
import re
import uuid
import xml.etree.ElementTree as ET
from math import cos, radians, sin
from pathlib import Path

HERE = Path(__file__).parent
DEFAULT_OUTPUT_DIR = HERE / "output" / "generated_panel"
DEFAULT_KICAD_PCB = HERE / "assets" / "KiCad_PCB" / "ES_Daisy_Patch_SM_FB_Rev1.kicad_pcb"
BOARD_SETTINGS_REFERENCE_PCB = HERE / "assets" / "KiCAD_Custom_DRC_Rules_for_PCBWay" / "KiCAD_Custom_DRC_Rules_for_PCBWay.kicad_pcb"
PATCH_INIT_PRETTY = HERE / "assets" / "KiCad_PCB" / "ES_Daisy_Patch_SM_FB_Rev1.pretty"
PATTERN_SVG = HERE / "pattern.svg"
PATTERN_B_SVG = HERE / "pattern_c.svg"
# Background layout: left PATTERN_ZONE_A_WIDTH = pattern.svg only; right PATTERN_ZONE_B_WIDTH = pattern_b.svg only; middle = smooth morph.
# Transition to B begins at panel midpoint: left half is pure A, then morph A→B from midpoint to right edge (or to start of pure B if ZONE_B > 0).
PATTERN_ZONE_A_WIDTH = 0.28
PATTERN_ZONE_B_WIDTH = 0.28 
PATTERN_MORPH_NUM_COLUMNS = 4  # blend columns in middle zone
PATTERN_MORPH_RESAMPLE_POINTS = 2048  # arc-length resample for morph
# Inset (mm) from zone A/B boundaries so morph does not overlap either; morph is drawn in [x_morph_start + inset_a, x_morph_end - inset_b]
PATTERN_MORPH_INSET_A_MM = 0   # gap between zone A and morph
PATTERN_MORPH_INSET_B_MM = 2   # gap between morph and zone B
# Clamp morph shape complexity (0.0–1.0): 1.0 = full detail; lower = fewer points, simpler shapes matching A/B density
PATTERN_MORPH_COMPLEXITY = 0.08
# Easing for morph blend: "linear" | "smoothstep" | "smootherstep" | "ease_in_out_sine"
# smoothstep/smootherstep ease at zone edges; ease_in_out_sine is very smooth.
PATTERN_MORPH_EASING = "linear"
# Boundary connection pass: connect discontinuous pattern lines at A|morph and morph|B. Tolerance (mm).
PATTERN_BOUNDARY_CONNECT_TOL_X = 0.1   # endpoint is "on" boundary if within this in x
# Connecting segments must move in +x from origin (no vertical); use this min delta when right x <= left x.
PATTERN_CONNECT_MIN_DX_MM = 5

# Copper pattern: trace width on F.Cu (mm)
PATTERN_TRACE_WIDTH_MM = 0.5
# Pattern scale per source: 1.0 = one tile per SVG size; 0.5 = half size (twice as many tiles per width/height)
PATTERN_SCALE_A = 0.2   # pattern.svg (left zone)
PATTERN_SCALE_B = 0.15   # pattern_b.svg (right zone)
# Extend pattern this far (mm) outside the board edge on all sides so copper extends past Edge.Cuts
PATTERN_EXTEND_OUTSIDE_MM = 10.0

# Panel size = patch_init gerbers (blank-Edge_Cuts.gbr): 3U × 10HP
PANEL_WIDTH_MM = 50.8
PANEL_HEIGHT_MM = 128.5

# KiCad PCB → panel-local coordinate offset (same as test_panel_alignment / hardware-centers-kicad)
KICAD_PANEL_OFFSET_X_MM = 123.10
KICAD_PANEL_OFFSET_Y_MM = 40.80

# Panel cutout diameters (mm) = drill + clearance from datasheets/README.md.
# These are the hole sizes in the panel and in hardware-centers-kicad.svg.
PANEL_MOUNT_MM = 3.0
PANEL_LED_MM = 3.2
PANEL_SWITCH_B7_MM = 5.5
PANEL_JACK_MM = 6.2 + 0.1   # 6.3
PANEL_POT_MM = 7.2 + 0.3    # 7.5
PANEL_SWITCH_B8_MM = 6.2 + 0.1  # 6.3 (same as jack)

# Eurorack screw slots
SCREW_WIDTH_MM = 5.0
SCREW_HEIGHT_MM = 3.0
SCREW_Y_TOP_MM = 3.0
SCREW_Y_BOTTOM_MM = PANEL_HEIGHT_MM - 3.0
SCREW_X_LEFT_MM = 7.50
SCREW_X_RIGHT_MM = 43.10
# When True: screw and SD slots are oblong drill slots (no Edge.Cuts cutouts). Validation checks rects contained in slots.
USE_DRILL_SLOTS = True
# Extra mm per dimension so drill slots fully contain the cutout rects (pad size = rect + this).
SLOT_OVERSIZE_MM = 0.2
# Mount (screw) drill slot height must not exceed this (mm).
MOUNT_SLOT_MAX_HEIGHT_MM = 3.0

# Grid origin (mm) for assembly diagram; panel is centered on this point
GRID_ORIGIN_X_MM = 150.0
GRID_ORIGIN_Y_MM = 100.0

# Offset so panel center (PANEL_WIDTH/2, PANEL_HEIGHT/2) lies at grid origin
PANEL_OFFSET_X_MM = GRID_ORIGIN_X_MM - PANEL_WIDTH_MM / 2.0
PANEL_OFFSET_Y_MM = GRID_ORIGIN_Y_MM - PANEL_HEIGHT_MM / 2.0

# Family identifier for parsed holes (and footprint choice)
FAMILY_POT = "pot"
FAMILY_JACK = "jack"
FAMILY_SWITCH_B7 = "switch_b7"
FAMILY_SWITCH_B8 = "switch_b8"
FAMILY_LED = "led"
FAMILY_MOUNT = "mount"

# Patch.Init footprint basename per family (for copying drawing primitives)
PATCH_FP_BY_FAMILY = {
    FAMILY_POT: "9MM_SNAP-IN_POT_SILK",
    FAMILY_JACK: "S_JACK",
    FAMILY_SWITCH_B7: "TL1105SPF250Q_SILK",
    FAMILY_SWITCH_B8: "S_JACK",  # same body as jack
    FAMILY_LED: "LED",
    FAMILY_MOUNT: None,
}

# Pad numbers matching KiCad_PCB source footprints (single cutout pad per panel footprint)
PAD_NUMBER_BY_FAMILY = {
    FAMILY_POT: "1",
    FAMILY_JACK: "1",
    FAMILY_LED: "1",
    FAMILY_SWITCH_B7: "1",
    FAMILY_SWITCH_B8: "1",  # TOGGLE pad 2 is at center (0 0)
    FAMILY_MOUNT: "1",
}

# --- Silkscreen text (same positions/fonts as _deprecated_generate_panel.py SILKSCREEN_TEMPLATE) ---
# Title lines (generated from constants; single color on PCB)
TITLE_TOP = "\u5316"  # 化
TITLE_BOTTOM = "RESYNTHESIS"
TITLE_X_MM = 25.4
TITLE_Y_TOP_MM = 8.0
TITLE_Y_BOTTOM_MM = 11.0
TITLE_SIZE_TOP_MM = 6.0
TITLE_SIZE_BOTTOM_MM = 3.6

# Component label: (panel_x_mm, panel_y_mm) -> (label_text, font_size_mm).
# Used to match labels to drill holes (nearest hole gets the label). Font size here
# is only a fallback; actual size is computed to fit without overlap.
SILK_LABEL_BY_POSITION: dict[tuple[float, float], tuple[str, float]] = {
    # Pot labels
    (11.176, 33): ("OFFER", 3.6),
    (39.65, 33): ("TIMESTRETCH", 3.6),
    (11.176, 52): ("FLUFF", 3.6),
    (39.65, 52): ("COLOR", 3.6),
    # Switch labels (B7 + B8): one label per hole (B7 = MAX COMP, B8 = PITCH LOCK)
    (8.65, 68.27): ("MAX COMP", 3.2),
    (25.503, 70.97): ("PITCH LOCK", 3.2),
    # Jack labels: THOUGHTS (!!!), then top/middle/bottom rows (13 jacks total)
    (0, 75): ("!!!", 3.6),  # first jack by (y,x) is at (42.151, 59.236)
    (7.15, 79.0): ("00", 3.6),
    (19.317, 79.0): ("01", 3.6),
    (31.483, 79.0): ("10", 3.6),
    (43.65, 79.0): ("11", 3.6),
    # Jack labels: middle row
    (7.15, 92.8): ("V/OCT", 3.6),
    (19.317, 92.8): ("SMOOTH", 3.6),
    (31.483, 92.8): ("SPRS", 3.6),
    (43.65, 92.8): ("D", 3.6),
    # Jack labels: bottom row
    (7.15, 106.4): ("IN L", 3.6),
    (19.317, 106.4): ("IN R", 3.6),
    (31.483, 106.4): ("OUT L", 3.6),
    (43.65, 106.4): ("OUT R", 3.6),
}

# Positions that should render in italic (subset of SILK_LABEL_BY_POSITION keys).
SILK_ITALIC_LABEL_POSITIONS: frozenset[tuple[float, float]] = frozenset({(43.65, 92.8)})

# Silkscreen fitting: labels centered under drill holes, size chosen to avoid overlap/overflow
SILK_LABEL_OFFSET_BELOW_MM = 2.5  # gap between bottom of drill hole and top of text
SILK_MARGIN_MM = 0.5  # margin from panel edge and between label bboxes
# Solder mask: only rectangles behind labels get mask (rest = exposed copper). Extra padding around label bbox.
MASK_LABEL_PAD_MM = 0.25
# Horizontal: extend at least this far past the leftmost/rightmost letters of the text (mm).
MASK_LABEL_EXTEND_H_MM = 1.5
# Assume rendered text can be this fraction wider than our width estimate (e.g. KiCad stroke font); expand mask accordingly.
MASK_LABEL_WIDTH_EXTRA_FRAC = 0.44
SILK_CHAR_WIDTH_RATIO = 0.55  # approximate width/height per character for width estimate
SILK_BODY_SIZE_MIN_MM = 0.6
SILK_BODY_SIZE_MAX_MM = 2
SILK_TITLE_SIZE_MAX_MM = 2.5  # max size for title lines at top
# Silkscreen font: face = TrueType font family name or "" for KiCad default stroke font
SILK_FONT_FACE = ""
SILK_FONT_BOLD = False
SILK_FONT_ITALIC = False
# Each is (center_x_mm, center_y_mm); box is 8×8 mm. Corner radius from SILK_OUTPUT_JACK_BOX_RADIUS_MM
# (KiCad gr_rect has no radius; we use gr_poly for rounded corners when radius > 0).
SILK_OUTPUT_JACK_BOXES_MM: list[tuple[float, float]] = [
    (42.155, 59.288),   # CV_OUT_1 / !!!
    (31.483, 84.562),   # B5
    (43.65, 84.562),    # B6
    (31.483, 111.9),    # OUT L
    (43.65, 111.9),     # OUT R
]
SILK_OUTPUT_JACK_BOX_SIZE_MM = 8.0  # width and height
SILK_OUTPUT_JACK_BOX_RADIUS_MM = 1.2  # corner radius (max = size/2 = 4 for 8×8 box); 0 = sharp gr_rect
SILK_OUTPUT_JACK_BOX_STROKE_MM = 0.2  # outline width on F.SilkS


def _estimate_text_width_mm(text: str, size_mm: float) -> float:
    """Approximate text width in mm for centering (KiCad default-style proportion)."""
    return max(0.5, len(text) * size_mm * SILK_CHAR_WIDTH_RATIO)


def _match_labels_to_holes(
    holes: list[tuple[float, float, str]],
) -> list[tuple[float, float, str, str, bool]]:
    """Return [(drill_x, drill_y, family, label_text, use_italic), ...] matching labels to holes by family and position.
    Excludes FAMILY_MOUNT. Labels are assigned to holes of the same family in order of (y, x).
    """
    by_family: dict[str, list[tuple[float, float, str]]] = {}
    for (hx, hy, fam) in holes:
        if fam == FAMILY_MOUNT:
            continue
        by_family.setdefault(fam, []).append((hx, hy, fam))
    for fam in by_family:
        by_family[fam].sort(key=lambda t: (t[1], t[0]))

    pot_labels = []
    switch_labels = []
    jack_labels = []
    for (lx, ly), (text, _) in SILK_LABEL_BY_POSITION.items():
        if ly < 60:
            pot_labels.append((lx, ly, text))
        elif ly < 75:
            switch_labels.append((lx, ly, text))
        else:
            jack_labels.append((lx, ly, text))
    pot_labels.sort(key=lambda t: (t[1], t[0]))
    switch_labels.sort(key=lambda t: (t[1], t[0]))
    jack_labels.sort(key=lambda t: (t[1], t[0]))

    out: list[tuple[float, float, str, str, bool]] = []
    for hole_list, label_list in [
        (by_family.get(FAMILY_POT, []), pot_labels),
        (sorted(by_family.get(FAMILY_SWITCH_B7, []) + by_family.get(FAMILY_SWITCH_B8, []), key=lambda t: (t[1], t[0])), switch_labels),
        (by_family.get(FAMILY_JACK, []), jack_labels),
    ]:
        for i, label in enumerate(label_list):
            if i < len(hole_list):
                hx, hy, fam = hole_list[i][0], hole_list[i][1], hole_list[i][2]
                lx, ly, text = label[0], label[1], label[2]
                use_italic = (lx, ly) in SILK_ITALIC_LABEL_POSITIONS
                out.append((hx, hy, fam, text, use_italic))
    return out


def _parse_holes_from_kicad(kicad_path: Path) -> list[tuple[float, float, str]]:
    """Return list of (x, y, family) in panel-local mm from Patch.Init KiCad PCB.
    family is one of FAMILY_POT, FAMILY_JACK, FAMILY_SWITCH_B7, FAMILY_SWITCH_B8, FAMILY_LED.
    """
    text = kicad_path.read_text(encoding="utf-8", errors="ignore")
    holes: list[tuple[float, float, str]] = []
    in_module = False
    depth = 0
    mod_ref: str | None = None
    mod_at_x, mod_at_y, mod_rot_deg = 0.0, 0.0, 0.0
    mod_footprint_name: str | None = None
    mod_have_at = False  # only use first (at) in module = module position, not pad

    def flush_module() -> None:
        nonlocal holes, mod_ref, mod_at_x, mod_at_y, mod_rot_deg, mod_footprint_name, mod_have_at
        if not in_module or mod_ref is None:
            return
        if not (
            mod_ref.startswith("J_")
            or mod_ref.startswith("VR_")
            or mod_ref.startswith("SW_")
            or mod_ref.startswith("LED")
        ):
            return
        theta = radians(mod_rot_deg)
        cx_rot = 0.0 * cos(theta) - 0.0 * sin(theta)
        cy_rot = 0.0 * sin(theta) + 0.0 * cos(theta)
        bx = mod_at_x + cx_rot
        by = mod_at_y + cy_rot
        lx = bx - KICAD_PANEL_OFFSET_X_MM
        ly = by - KICAD_PANEL_OFFSET_Y_MM
        family: str | None = None
        if mod_footprint_name:
            fp_upper = mod_footprint_name.upper().split(":")[-1]
            if "9MM_SNAP-IN_POT" in fp_upper:
                family = FAMILY_POT
            elif "S_JACK" in fp_upper:
                family = FAMILY_JACK
            elif "TL1105" in fp_upper:
                family = FAMILY_SWITCH_B7
            elif "TOGGLE" in fp_upper and "ON-ON" in fp_upper:
                family = FAMILY_SWITCH_B8
            elif "LED" in fp_upper:
                family = FAMILY_LED
        if family is None:
            return
        holes.append((lx, ly, family))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        open_p, close_p = line.count("("), line.count(")")
        if not in_module and line.startswith("(module "):
            in_module = True
            depth = open_p - close_p
            parts = line.split()
            mod_footprint_name = parts[1] if len(parts) >= 2 else None
            mod_ref = None
            mod_at_x, mod_at_y, mod_rot_deg = 0.0, 0.0, 0.0
            mod_have_at = False
            continue
        if in_module:
            # Only use first (at) in module = module position; later (at) are pads/fp_text.
            if line.startswith("(at ") and not mod_have_at:
                tok = line.strip("()").split()
                if len(tok) >= 3:
                    try:
                        mod_at_x, mod_at_y = float(tok[1]), float(tok[2])
                        mod_rot_deg = float(tok[3]) if len(tok) >= 4 else 0.0
                        mod_have_at = True
                    except ValueError:
                        pass
            if line.startswith("(fp_text reference "):
                parts = line.split()
                if len(parts) >= 3:
                    ref = parts[2].strip('"')
                    mod_ref = ref
            depth += open_p - close_p
            if depth <= 0:
                flush_module()
                in_module = False
    if in_module:
        flush_module()
    return holes


def _mount_holes() -> list[tuple[float, float, str]]:
    """Four Eurorack mounting holes (panel-local mm)."""
    centers = [
        (SCREW_X_LEFT_MM, SCREW_Y_TOP_MM),
        (SCREW_X_RIGHT_MM, SCREW_Y_TOP_MM),
        (SCREW_X_LEFT_MM, SCREW_Y_BOTTOM_MM),
        (SCREW_X_RIGHT_MM, SCREW_Y_BOTTOM_MM),
    ]
    return [(cx, cy, FAMILY_MOUNT) for cx, cy in centers]


def _parse_sd_slot_from_kicad(kicad_path: Path) -> tuple[float, float, float, float] | None:
    """Return (x, y, w, h) panel-local mm for SD slot from U_SDCARD1, or None."""
    text = kicad_path.read_text(encoding="utf-8", errors="ignore")
    in_module, depth = False, 0
    mod_ref, mod_at_x, mod_at_y, mod_rot_deg = None, 0.0, 0.0, 0.0
    sd_center: tuple[float, float] | None = None

    def flush() -> tuple[float, float] | None:
        if not in_module or mod_ref != "U_SDCARD1":
            return None
        return (mod_at_x - KICAD_PANEL_OFFSET_X_MM, mod_at_y - KICAD_PANEL_OFFSET_Y_MM)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        open_p, close_p = line.count("("), line.count(")")
        if not in_module and line.startswith("(module "):
            in_module, depth = True, open_p - close_p
            mod_ref, mod_at_x, mod_at_y, mod_rot_deg = None, 0.0, 0.0, 0.0
            continue
        if in_module:
            if line.startswith("(at "):
                tok = line.strip("()").split()
                if len(tok) >= 3:
                    try:
                        mod_at_x, mod_at_y = float(tok[1]), float(tok[2])
                        mod_rot_deg = float(tok[3]) if len(tok) >= 4 else 0.0
                    except ValueError:
                        pass
            if line.startswith("(fp_text reference "):
                parts = line.split()
                if len(parts) >= 3:
                    mod_ref = parts[2].strip('"')
            depth += open_p - close_p
            if depth <= 0:
                if sd_center is None:
                    sd_center = flush()
                in_module = False
    if in_module and sd_center is None:
        sd_center = flush()
    if sd_center is None:
        return None
    cx, cy = sd_center
    w, h = 2.3, 12.0
    return (cx - w / 2.0, cy - h / 2.0, w, h)


def _screw_slot_rects() -> list[tuple[float, float, float, float]]:
    """Return [(x, y, w, h), ...] for the four Eurorack screw slots."""
    centers = [
        (SCREW_X_LEFT_MM, SCREW_Y_TOP_MM),
        (SCREW_X_RIGHT_MM, SCREW_Y_TOP_MM),
        (SCREW_X_LEFT_MM, SCREW_Y_BOTTOM_MM),
        (SCREW_X_RIGHT_MM, SCREW_Y_BOTTOM_MM),
    ]
    return [
        (cx - SCREW_WIDTH_MM / 2.0, cy - SCREW_HEIGHT_MM / 2.0, SCREW_WIDTH_MM, SCREW_HEIGHT_MM)
        for cx, cy in centers
    ]


def _panel_cutout_diameter_mm(family: str) -> float:
    """Panel cutout diameter (mm) for the given family (README / footprint_calc)."""
    return {
        FAMILY_MOUNT: PANEL_MOUNT_MM,
        FAMILY_LED: PANEL_LED_MM,
        FAMILY_SWITCH_B7: PANEL_SWITCH_B7_MM,
        FAMILY_JACK: PANEL_JACK_MM,
        FAMILY_POT: PANEL_POT_MM,
        FAMILY_SWITCH_B8: PANEL_SWITCH_B8_MM,
    }[family]


def _footprint_name(family: str) -> str:
    """Library footprint name for the panel version (no lib prefix)."""
    d = _panel_cutout_diameter_mm(family)
    d_str = str(d).replace(".", "p")
    return {
        FAMILY_MOUNT: f"Panel_Mount_{d_str}mm",
        FAMILY_LED: f"Panel_LED_{d_str}mm",
        FAMILY_SWITCH_B7: f"Panel_Switch_B7_{d_str}mm",
        FAMILY_JACK: f"Panel_Jack_{d_str}mm",
        FAMILY_POT: f"Panel_9MM_Pot_{d_str}mm",
        FAMILY_SWITCH_B8: f"Panel_Switch_B8_{d_str}mm",
    }[family]


def _slot_footprint_name(w_mm: float, h_mm: float) -> str:
    """Footprint name for an oblong drill slot (e.g. Panel_Slot_5p0x3p0)."""
    w_str = str(round(w_mm, 2)).replace(".", "p")
    h_str = str(round(h_mm, 2)).replace(".", "p")
    return f"Panel_Slot_{w_str}x{h_str}"


def _write_slot_footprint(
    pretty_path: Path,
    w_mm: float,
    h_mm: float,
    lib_name: str,
) -> None:
    """Write a footprint with a single oval thru_hole pad (drill slot). Size = (w_mm, h_mm), minimal to fit the slot."""
    mod_name = _slot_footprint_name(w_mm, h_mm)
    mod_file = pretty_path / f"{mod_name}.kicad_mod"
    pad_uuid = _uuid_full()
    ref_uuid = _uuid_full()
    val_uuid = _uuid_full()
    content = f'''(footprint "{mod_name}" (version 20221018) (generator "resynthesis-panel")
  (layer "F.Cu")
  (at 0 0)
  (fp_text reference "" (at 0 0) (layer F.SilkS) hide
    (effects (font (size 1 1) (thickness 0.15)))
    (uuid "{ref_uuid}"))
  (fp_text value "" (at 0 0) (layer F.SilkS) hide
    (effects (font (size 1 1) (thickness 0.15)))
    (uuid "{val_uuid}"))
  (pad "1" thru_hole oval (at 0 0) (size {w_mm:.4f} {h_mm:.4f}) (drill oval {w_mm:.4f} {h_mm:.4f}) (layers *.Cu *.Mask)
    (uuid "{pad_uuid}"))
)
'''
    mod_file.write_text(content, encoding="utf-8")


def _slot_footprint_body_lines(w_mm: float, h_mm: float, indent: str = "    ") -> list[str]:
    """Return footprint body lines for a slot (oval pad) for embedding in the PCB."""
    pad_uuid = _uuid_full()
    ref_uuid = _uuid_full()
    val_uuid = _uuid_full()
    return [
        f'{indent}(fp_text reference "" (at 0 0) (layer F.SilkS) hide',
        f'{indent}  (effects (font (size 1 1) (thickness 0.15)))',
        f'{indent}  (uuid "{ref_uuid}"))',
        f'{indent}(fp_text value "" (at 0 0) (layer F.SilkS) hide',
        f'{indent}  (effects (font (size 1 1) (thickness 0.15)))',
        f'{indent}  (uuid "{val_uuid}"))',
        f'{indent}(pad "1" thru_hole oval (at 0 0) (size {w_mm:.4f} {h_mm:.4f}) (drill oval {w_mm:.4f} {h_mm:.4f}) (layers *.Cu *.Mask)',
        f'{indent}  (uuid "{pad_uuid}"))',
    ]


def _uuid() -> str:
    return uuid.uuid4().hex[:8].upper()


def _uuid_full() -> str:
    return str(uuid.uuid4()).upper()


def _extract_drawing_primitives(kicad_mod_path: Path) -> list[str]:
    """Extract fp_line, fp_circle, fp_arc, fp_poly from a .kicad_mod.
    These form the component outline (black strokes in footprint_calc/*_panel_overlay.svg).
    Layer is set to Dwgs.User for the panel footprint drawing layer.
    """
    if not kicad_mod_path.exists():
        return []
    text = kicad_mod_path.read_text(encoding="utf-8", errors="ignore")
    drawing_prefixes = ("(fp_line ", "(fp_circle ", "(fp_arc ", "(fp_poly ")
    result: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not any(line.startswith(p) for p in drawing_prefixes):
            continue
        # Replace (layer X) with (layer Dwgs.User)
        layer_re = re.sub(r"\(layer\s+\S+\)", "(layer Dwgs.User)", line)
        result.append(layer_re)
    return result


def _write_panel_footprint(
    pretty_path: Path,
    family: str,
    lib_name: str,
) -> None:
    """Write one panel footprint: NPTH = panel cutout (README), Dwgs.User = component outline (footprint_calc)."""
    mod_name = _footprint_name(family)
    cutout_mm = _panel_cutout_diameter_mm(family)
    mod_file = pretty_path / f"{mod_name}.kicad_mod"

    pad_uuid = _uuid_full()
    ref_uuid = _uuid_full()
    val_uuid = _uuid_full()

    drawing_lines: list[str] = []
    patch_fp = PATCH_FP_BY_FAMILY.get(family)
    if patch_fp:
        fp_path = PATCH_INIT_PRETTY / f"{patch_fp}.kicad_mod"
        drawing_lines = _extract_drawing_primitives(fp_path)

    indent = "  "
    # Mount: no visible pad (no pad at all for compatibility); center marker on Dwgs.User only
    mount_center_marker = (
        f'{indent}(fp_circle (center 0 0) (end 0.5 0) (layer Dwgs.User) (width 0.1))'
    )

    blocks = [
        f'(footprint "{mod_name}" (version 20221018) (generator "resynthesis-panel")',
        '  (layer "F.Cu")',
        "  (at 0 0)",
        '  (fp_text reference "" (at 0 0) (layer F.SilkS) hide',
        "    (effects (font (size 1 1) (thickness 0.15)))",
        f'    (uuid "{ref_uuid}"))',
        '  (fp_text value "" (at 0 0) (layer F.SilkS) hide',
        "    (effects (font (size 1 1) (thickness 0.15)))",
        f'    (uuid "{val_uuid}"))',
    ]
    if family == FAMILY_MOUNT:
        blocks.append(mount_center_marker)
    else:
        pad_block = (
            f'{indent}(pad "{PAD_NUMBER_BY_FAMILY[family]}" thru_hole circle (at 0 0) (size {cutout_mm:.4f} {cutout_mm:.4f}) '
            f'(drill {cutout_mm:.4f}) (layers *.Cu *.Mask)\n'
            f'{indent}  (uuid "{pad_uuid}"))'
        )
        blocks.append(pad_block)
    for line in drawing_lines:
        blocks.append(indent + line)
    blocks.append(")")

    mod_file.write_text("\n".join(blocks) + "\n", encoding="utf-8")


def _footprint_body_lines(family: str, indent: str = "    ") -> list[str]:
    """Return the footprint body (fp_text, pad or mount center marker, drawing primitives) for embedding in the PCB.
    Each call uses fresh UUIDs so each instance is valid.
    Mount footprints have no pad; they have a Dwgs.User circle at center instead.
    """
    cutout_mm = _panel_cutout_diameter_mm(family)
    pad_uuid = _uuid_full()
    ref_uuid = _uuid_full()
    val_uuid = _uuid_full()

    drawing_lines: list[str] = []
    patch_fp = PATCH_FP_BY_FAMILY.get(family)
    if patch_fp:
        fp_path = PATCH_INIT_PRETTY / f"{patch_fp}.kicad_mod"
        drawing_lines = _extract_drawing_primitives(fp_path)

    lines = [
        f'{indent}(fp_text reference "" (at 0 0) (layer F.SilkS) hide',
        f'{indent}  (effects (font (size 1 1) (thickness 0.15)))',
        f'{indent}  (uuid "{ref_uuid}"))',
        f'{indent}(fp_text value "" (at 0 0) (layer F.SilkS) hide',
        f'{indent}  (effects (font (size 1 1) (thickness 0.15)))',
        f'{indent}  (uuid "{val_uuid}"))',
    ]
    if family == FAMILY_MOUNT:
        lines.append(f'{indent}(fp_circle (center 0 0) (end 0.5 0) (layer Dwgs.User) (width 0.1))')
    else:
        lines.append(
            f'{indent}(pad "{PAD_NUMBER_BY_FAMILY[family]}" thru_hole circle (at 0 0) (size {cutout_mm:.4f} {cutout_mm:.4f}) '
            f'(drill {cutout_mm:.4f}) (layers *.Cu *.Mask)'
        )
        lines.append(f'{indent}  (uuid "{pad_uuid}"))')
    for dl in drawing_lines:
        lines.append(indent + dl)
    return lines


def _tokenize_svg_path(d: str) -> list[str]:
    """Tokenize SVG path d: split into command letters and numbers."""
    d = d.replace(",", " ")
    tokens: list[str] = []
    # One number: optional minus, then either digits.digits or .digits (single dot only)
    num_re = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
    pos = 0
    while pos < len(d):
        if d[pos] in " \t\n\r":
            pos += 1
            continue
        if d[pos] in "MmLlHhVvCcSsQqTtAaZz":
            tokens.append(d[pos])
            pos += 1
            continue
        m = num_re.match(d[pos:])
        if m:
            tokens.append(m.group(0))
            pos += m.end()
            continue
        pos += 1
    return tokens


def _svg_arc_to_center(
    x1: float, y1: float,
    rx: float, ry: float,
    phi_deg: float,
    fa: int, fs: int,
    x2: float, y2: float,
) -> tuple[float, float, float, float] | None:
    """Convert SVG arc to center parameterization. Returns (cx, cy, theta1_rad, d_theta_rad) or None if degenerate."""
    if rx <= 0 or ry <= 0:
        return None
    phi = radians(phi_deg)
    cos_phi = cos(phi)
    sin_phi = sin(phi)
    dx = (x1 - x2) / 2.0
    dy = (y1 - y2) / 2.0
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    lambda_ = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lambda_ > 1:
        rx = math.sqrt(lambda_) * rx
        ry = math.sqrt(lambda_) * ry
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    if abs(den) < 1e-12:
        return None
    sq = (rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p) / den
    if sq < 0:
        return None
    coef = (1 if fa != fs else -1) * math.sqrt(max(0, sq))
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0
    ux = (x1p - cxp) / rx
    uy = (y1p - cyp) / ry
    vx = (-x1p - cxp) / rx
    vy = (-y1p - cyp) / ry
    theta1 = math.atan2(uy, ux)
    d_theta = math.atan2(vy, vx) - theta1
    if fs == 0 and d_theta > 0:
        d_theta -= 2.0 * math.pi
    if fs == 1 and d_theta < 0:
        d_theta += 2.0 * math.pi
    return (cx, cy, theta1, d_theta)


def _parse_svg_path_to_primitives(d: str) -> list[tuple[str, ...]]:
    """Parse path into ('segment', x1,y1,x2,y2) or ('arc', xs,ys, xm,ym, xe,ye). Preserves SVG arcs (A/a) as arcs."""
    tokens = _tokenize_svg_path(d)
    prims: list[tuple[str, ...]] = []
    idx = [0]

    def read_num() -> float:
        if idx[0] >= len(tokens) or tokens[idx[0]] in "MmLlHhVvCcSsQqTtAaZz":
            raise ValueError("expected number")
        v = float(tokens[idx[0]])
        idx[0] += 1
        return v

    cur_x, cur_y = 0.0, 0.0
    start_x, start_y = 0.0, 0.0

    while idx[0] < len(tokens):
        cmd = tokens[idx[0]]
        idx[0] += 1
        if cmd == "M":
            cur_x, cur_y = read_num(), read_num()
            start_x, start_y = cur_x, cur_y
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                x, y = read_num(), read_num()
                prims.append(("segment", cur_x, cur_y, x, y))
                cur_x, cur_y = x, y
        elif cmd == "m":
            cur_x += read_num()
            cur_y += read_num()
            start_x, start_y = cur_x, cur_y
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                dx, dy = read_num(), read_num()
                prims.append(("segment", cur_x, cur_y, cur_x + dx, cur_y + dy))
                cur_x += dx
                cur_y += dy
        elif cmd == "L":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                x, y = read_num(), read_num()
                prims.append(("segment", cur_x, cur_y, x, y))
                cur_x, cur_y = x, y
        elif cmd == "l":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                dx, dy = read_num(), read_num()
                prims.append(("segment", cur_x, cur_y, cur_x + dx, cur_y + dy))
                cur_x += dx
                cur_y += dy
        elif cmd == "H":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                x = read_num()
                prims.append(("segment", cur_x, cur_y, x, cur_y))
                cur_x = x
        elif cmd == "h":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                dx = read_num()
                prims.append(("segment", cur_x, cur_y, cur_x + dx, cur_y))
                cur_x += dx
        elif cmd == "V":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                y = read_num()
                prims.append(("segment", cur_x, cur_y, cur_x, y))
                cur_y = y
        elif cmd == "v":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                dy = read_num()
                prims.append(("segment", cur_x, cur_y, cur_x, cur_y + dy))
                cur_y += dy
        elif cmd in "zZ":
            prims.append(("segment", cur_x, cur_y, start_x, start_y))
            cur_x, cur_y = start_x, start_y
        elif cmd in "Cc":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                read_num()
                read_num()
                read_num()
                read_num()
                x, y = read_num(), read_num()
                prims.append(("segment", cur_x, cur_y, x, y))
                cur_x, cur_y = x, y
        elif cmd in "SsQqTt":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                if cmd in "Qq":
                    read_num()
                    read_num()
                x, y = read_num(), read_num()
                prims.append(("segment", cur_x, cur_y, x, y))
                cur_x, cur_y = x, y
        elif cmd in "Aa":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                rx, ry = read_num(), read_num()
                phi = read_num()
                fa = int(read_num())
                fs = int(read_num())
                x2, y2 = read_num(), read_num()
                if cmd == "a":
                    x2 += cur_x
                    y2 += cur_y
                result = _svg_arc_to_center(cur_x, cur_y, rx, ry, phi, fa, fs, x2, y2)
                if result is not None:
                    cx, cy, theta1, d_theta = result
                    r = rx
                    xs = cx + r * cos(theta1)
                    ys = cy + r * sin(theta1)
                    xe = cx + r * cos(theta1 + d_theta)
                    ye = cy + r * sin(theta1 + d_theta)
                    theta_mid = theta1 + d_theta / 2.0
                    xm = cx + r * cos(theta_mid)
                    ym = cy + r * sin(theta_mid)
                    prims.append(("arc", xs, ys, xm, ym, xe, ye))
                else:
                    prims.append(("segment", cur_x, cur_y, x2, y2))
                cur_x, cur_y = x2, y2
    return prims


def _circle_through_three_points(
    x1: float, y1: float, x2: float, y2: float, x3: float, y3: float
) -> tuple[float, float, float] | None:
    """Return (cx, cy, r) of circle through the three points, or None if collinear."""
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    return (ux, uy, r)


def _arc_length_and_sample(
    xs: float, ys: float, xm: float, ym: float, xe: float, ye: float, t: float
) -> tuple[float, float]:
    """Given arc (start, mid, end), return point at fraction t in [0,1] along the arc."""
    circ = _circle_through_three_points(xs, ys, xm, ym, xe, ye)
    if circ is None:
        return (xs + t * (xe - xs), ys + t * (ye - ys))
    cx, cy, r = circ
    theta_s = math.atan2(ys - cy, xs - cx)
    theta_m = math.atan2(ym - cy, xm - cx)
    theta_e = math.atan2(ye - cy, xe - cx)
    # Sweep from start to end that passes through mid
    d_sm = (theta_m - theta_s + 3 * math.pi) % (2 * math.pi) - math.pi
    d_me = (theta_e - theta_m + 3 * math.pi) % (2 * math.pi) - math.pi
    sweep = d_sm + d_me
    theta = theta_s + t * sweep
    return (cx + r * cos(theta), cy + r * sin(theta))


def _resample_primitives_arc_length(
    primitives: list[tuple[str, ...]],
    n: int,
    tile_w: float,
    tile_h: float,
    closed: bool = True,
) -> list[tuple[float, float]]:
    """Resample path made of segments and arcs to n points by arc length. Returns points in 0..1 normalized coords."""
    if n <= 0 or not primitives or tile_w <= 0 or tile_h <= 0:
        return []
    lengths: list[float] = []
    for prim in primitives:
        if prim[0] == "segment":
            x1, y1, x2, y2 = prim[1], prim[2], prim[3], prim[4]
            lengths.append(math.hypot(x2 - x1, y2 - y1))
        else:
            xs, ys, xm, ym, xe, ye = prim[1], prim[2], prim[3], prim[4], prim[5], prim[6]
            circ = _circle_through_three_points(xs, ys, xm, ym, xe, ye)
            if circ is None:
                lengths.append(math.hypot(xe - xs, ye - ys))
            else:
                cx, cy, r = circ
                theta_s = math.atan2(ys - cy, xs - cx)
                theta_m = math.atan2(ym - cy, xm - cx)
                theta_e = math.atan2(ye - cy, xe - cx)
                d_sm = (theta_m - theta_s + 3 * math.pi) % (2 * math.pi) - math.pi
                d_me = (theta_e - theta_m + 3 * math.pi) % (2 * math.pi) - math.pi
                lengths.append(r * abs(d_sm + d_me))
    total = sum(lengths)
    close_len = 0.0
    if closed and primitives:
        if primitives[0][0] == "segment":
            sx, sy = primitives[0][1], primitives[0][2]
        else:
            sx, sy = primitives[0][1], primitives[0][2]
        if primitives[-1][0] == "segment":
            ex, ey = primitives[-1][3], primitives[-1][4]
        else:
            ex, ey = primitives[-1][5], primitives[-1][6]
        close_len = math.hypot(sx - ex, sy - ey)
        total += close_len
    if total <= 0:
        return []

    def point_at_s(s: float) -> tuple[float, float]:
        s = s % total if closed else max(0, min(s, total))
        acc = 0.0
        for idx, prim in enumerate(primitives):
            L = lengths[idx]
            if acc <= s < acc + L:
                t = (s - acc) / L if L > 0 else 0.0
                if prim[0] == "segment":
                    x1, y1, x2, y2 = prim[1], prim[2], prim[3], prim[4]
                    x = x1 + t * (x2 - x1)
                    y = y1 + t * (y2 - y1)
                else:
                    x, y = _arc_length_and_sample(prim[1], prim[2], prim[3], prim[4], prim[5], prim[6], t)
                return (x / tile_w, y / tile_h)
            acc += L
        # closing segment
        if primitives[-1][0] == "segment":
            ex, ey = primitives[-1][3], primitives[-1][4]
        else:
            ex, ey = primitives[-1][5], primitives[-1][6]
        sx, sy = primitives[0][1], primitives[0][2]
        t = (s - acc) / close_len if close_len > 0 else 0.0
        x = ex + t * (sx - ex)
        y = ey + t * (sy - ey)
        return (x / tile_w, y / tile_h)

    out: list[tuple[float, float]] = []
    for k in range(n):
        s = (k * total / n) % total if closed else (k * total / (n - 1) if n > 1 else 0)
        out.append(point_at_s(s))
    return out


def _parse_svg_path_to_segments(d: str) -> list[tuple[float, float, float, float]]:
    """Parse path d into line segments (x1, y1, x2, y2). Handles M,L,H,V,m,l,h,v,z; curves approx as line."""
    tokens = _tokenize_svg_path(d)
    segments: list[tuple[float, float, float, float]] = []
    idx = [0]

    def read_num() -> float:
        if idx[0] >= len(tokens) or tokens[idx[0]] in "MmLlHhVvCcSsQqTtAaZz":
            raise ValueError("expected number")
        v = float(tokens[idx[0]])
        idx[0] += 1
        return v

    def add_line(x1: float, y1: float, x2: float, y2: float) -> None:
        segments.append((x1, y1, x2, y2))

    cur_x, cur_y = 0.0, 0.0
    start_x, start_y = 0.0, 0.0

    while idx[0] < len(tokens):
        cmd = tokens[idx[0]]
        idx[0] += 1
        if cmd == "M":
            cur_x, cur_y = read_num(), read_num()
            start_x, start_y = cur_x, cur_y
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                x, y = read_num(), read_num()
                add_line(cur_x, cur_y, x, y)
                cur_x, cur_y = x, y
        elif cmd == "m":
            cur_x += read_num()
            cur_y += read_num()
            start_x, start_y = cur_x, cur_y
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                dx, dy = read_num(), read_num()
                add_line(cur_x, cur_y, cur_x + dx, cur_y + dy)
                cur_x += dx
                cur_y += dy
        elif cmd == "L":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                x, y = read_num(), read_num()
                add_line(cur_x, cur_y, x, y)
                cur_x, cur_y = x, y
        elif cmd == "l":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                dx, dy = read_num(), read_num()
                add_line(cur_x, cur_y, cur_x + dx, cur_y + dy)
                cur_x += dx
                cur_y += dy
        elif cmd == "H":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                x = read_num()
                add_line(cur_x, cur_y, x, cur_y)
                cur_x = x
        elif cmd == "h":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                dx = read_num()
                add_line(cur_x, cur_y, cur_x + dx, cur_y)
                cur_x += dx
        elif cmd == "V":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                y = read_num()
                add_line(cur_x, cur_y, cur_x, y)
                cur_y = y
        elif cmd == "v":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                dy = read_num()
                add_line(cur_x, cur_y, cur_x, cur_y + dy)
                cur_y += dy
        elif cmd in "zZ":
            add_line(cur_x, cur_y, start_x, start_y)
            cur_x, cur_y = start_x, start_y
        elif cmd in "Cc":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                read_num()
                read_num()
                read_num()
                read_num()
                x, y = read_num(), read_num()
                add_line(cur_x, cur_y, x, y)
                cur_x, cur_y = x, y
        elif cmd in "SsQqTt":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                if cmd in "Qq":
                    read_num()
                    read_num()
                x, y = read_num(), read_num()
                add_line(cur_x, cur_y, x, y)
                cur_x, cur_y = x, y
        elif cmd in "Aa":
            while idx[0] < len(tokens) and tokens[idx[0]] not in "MmLlHhVvCcSsQqTtAaZz":
                for _ in range(5):
                    read_num()
                x, y = read_num(), read_num()
                add_line(cur_x, cur_y, x, y)
                cur_x, cur_y = x, y
    return segments


def _clip_segment_to_rect(
    x1: float, y1: float, x2: float, y2: float,
    rx_min: float, ry_min: float, rx_max: float, ry_max: float,
) -> list[tuple[float, float, float, float]]:
    """Clip line to rectangle. Returns 0 or 1 segment."""
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return []
    t0, t1 = 0.0, 1.0
    for (edge, q) in [
        (-dx, x1 - rx_min),
        (dx, rx_max - x1),
        (-dy, y1 - ry_min),
        (dy, ry_max - y1),
    ]:
        if edge == 0:
            if q < 0:
                return []
            continue
        t = q / edge
        if edge < 0:
            if t > t1:
                return []
            t0 = max(t0, t)
        else:
            if t < t0:
                return []
            t1 = min(t1, t)
    if t0 >= t1:
        return []
    return [(x1 + t0 * dx, y1 + t0 * dy, x1 + t1 * dx, y1 + t1 * dy)]


def _segments_to_ordered_polyline(
    segments: list[tuple[float, float, float, float]],
) -> list[tuple[float, float]]:
    """Build ordered polyline from segments (draw order). Returns list of (x, y)."""
    if not segments:
        return []
    points: list[tuple[float, float]] = []
    tol = 1e-6
    for (x1, y1, x2, y2) in segments:
        if not points:
            points.append((x1, y1))
        else:
            px, py = points[-1]
            if abs(px - x1) > tol or abs(py - y1) > tol:
                points.append((x1, y1))
        points.append((x2, y2))
    return points


def _resample_polyline_arc_length(
    points: list[tuple[float, float]],
    n: int,
    closed: bool = True,
) -> list[tuple[float, float]]:
    """Resample polyline to n points by arc length. If closed, treat first and last as connected."""
    if n <= 0 or not points:
        return []
    if len(points) == 1:
        return [points[0]] * n
    # Compute cumulative lengths (between consecutive points)
    lengths: list[float] = [0.0]
    for i in range(1, len(points)):
        ax, ay = points[i - 1]
        bx, by = points[i]
        lengths.append(lengths[-1] + math.hypot(bx - ax, by - ay))
    if closed:
        ax, ay = points[-1]
        bx, by = points[0]
        total = lengths[-1] + math.hypot(bx - ax, by - ay)
    else:
        total = lengths[-1]
    if total <= 0:
        return points[:n] if len(points) >= n else points
    out: list[tuple[float, float]] = []
    for k in range(n):
        s = (k * total / n) % total if closed else (k * total / (n - 1) if n > 1 else 0)
        # Find segment containing s (including closing segment when closed)
        if closed and s >= lengths[-1]:
            # Closing segment: last point -> first point
            ax, ay = points[-1]
            bx, by = points[0]
            seg_len = math.hypot(bx - ax, by - ay)
            t = (s - lengths[-1]) / seg_len if seg_len > 0 else 0.0
            out.append((ax + t * (bx - ax), ay + t * (by - ay)))
            continue
        for i in range(len(lengths) - 1):
            if lengths[i] <= s <= lengths[i + 1]:
                t = (s - lengths[i]) / (lengths[i + 1] - lengths[i]) if lengths[i + 1] > lengths[i] else 0.0
                ax, ay = points[i]
                bx, by = points[i + 1]
                out.append((ax + t * (bx - ax), ay + t * (by - ay)))
                break
        else:
            out.append(points[-1])
    return out


def _load_pattern_normalized_resampled(
    svg_path: Path,
    n_points: int,
) -> tuple[list[tuple[float, float]], float, float]:
    """Load pattern SVG path, normalize to 0..1 tile, resample to n points by arc length. Returns (points, tile_w, tile_h).
    Uses primitives (arcs + segments) when available so resampling follows curves; falls back to segment-only.
    """
    if not svg_path.exists() or n_points < 2:
        return [], 0.0, 0.0
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ns = "{http://www.w3.org/2000/svg}"
    w_attr = root.get("width", "0").replace("px", "").strip()
    h_attr = root.get("height", "0").replace("px", "").strip()
    tile_w = float(w_attr) if w_attr else 23.07
    tile_h = float(h_attr) if h_attr else 40.0
    path_d = None
    for elem in root.iter(f"{ns}path"):
        path_d = elem.get("d")
        if path_d:
            break
    if not path_d:
        return [], tile_w, tile_h
    try:
        prims = _parse_svg_path_to_primitives(path_d)
        if prims:
            resampled = _resample_primitives_arc_length(prims, n_points, tile_w, tile_h, closed=True)
            if resampled:
                return resampled, tile_w, tile_h
    except (ValueError, ZeroDivisionError):
        pass
    raw = _parse_svg_path_to_segments(path_d)
    poly = _segments_to_ordered_polyline(raw)
    if not poly:
        return [], tile_w, tile_h
    norm = [(x / tile_w, y / tile_h) for (x, y) in poly]
    resampled = _resample_polyline_arc_length(norm, n_points, closed=True)
    return resampled, tile_w, tile_h


def _load_pattern_arc_ratio(svg_path: Path) -> float:
    """Return fraction of path primitives that are arcs (0 = angular, 1 = all arcs). Used to target B's curvature in morph."""
    if not svg_path.exists():
        return 0.0
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ns = "{http://www.w3.org/2000/svg}"
    path_d = None
    for elem in root.iter(f"{ns}path"):
        path_d = elem.get("d")
        if path_d:
            break
    if not path_d:
        return 0.0
    try:
        prims = _parse_svg_path_to_primitives(path_d)
        return _arc_ratio_from_primitives(prims)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _interpolate_points(
    pa: list[tuple[float, float]],
    pb: list[tuple[float, float]],
    t: float,
) -> list[tuple[float, float]]:
    """Linear interpolation between two point lists (same length). t=0 -> pa, t=1 -> pb."""
    n = min(len(pa), len(pb))
    if n == 0:
        return []
    return [
        ((1 - t) * pa[i][0] + t * pb[i][0], (1 - t) * pa[i][1] + t * pb[i][1])
        for i in range(n)
    ]


def _signed_area_closed(points: list[tuple[float, float]]) -> float:
    """Signed area of closed polygon (shoelace). Positive = CCW, negative = CW."""
    if len(points) < 3:
        return 0.0
    a = 0.0
    for i in range(len(points)):
        j = (i + 1) % len(points)
        a += points[i][0] * points[j][1] - points[j][0] * points[i][1]
    return a * 0.5


def _normalize_morph_winding(
    pa: list[tuple[float, float]],
    pb: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Ensure both closed paths have the same winding (CCW). Returns (pa', pb') with one reversed if needed."""
    if len(pa) < 3 or len(pb) < 3:
        return pa, pb
    sa = _signed_area_closed(pa)
    sb = _signed_area_closed(pb)
    if sa * sb < 0:
        pb = list(reversed(pb))
    return pa, pb


def _align_morph_starts(
    pa: list[tuple[float, float]],
    pb: list[tuple[float, float]],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Rotate pb so that its start index aligns with pa's start (same phase along the curve). Keeps morph continuous."""
    n = min(len(pa), len(pb))
    if n < 3:
        return pa, pb
    # Centroid of each path
    cx_a = sum(p[0] for p in pa[:n]) / n
    cy_a = sum(p[1] for p in pa[:n]) / n
    cx_b = sum(p[0] for p in pb[:n]) / n
    cy_b = sum(p[1] for p in pb[:n]) / n
    angle_a0 = math.atan2(pa[0][1] - cy_a, pa[0][0] - cx_a)
    # Find k in pb that minimizes angular difference to angle_a0
    best_k = 0
    best_diff = 10.0
    for k in range(n):
        angle_bk = math.atan2(pb[k][1] - cy_b, pb[k][0] - cx_b)
        diff = abs((angle_bk - angle_a0 + 3 * math.pi) % (2 * math.pi) - math.pi)
        if diff < best_diff:
            best_diff = diff
            best_k = k
    if best_k == 0:
        return pa, pb
    pb_rotated = [pb[(best_k + i) % n] for i in range(n)]
    return pa, pb_rotated


def _subsample_polyline_closed(
    points: list[tuple[float, float]],
    target_count: int,
) -> list[tuple[float, float]]:
    """Return evenly spaced subset of closed polyline. target_count = desired number of points (min 3)."""
    n = len(points)
    if n <= 0 or target_count >= n:
        return list(points)
    k = max(3, min(target_count, n))
    step = n / k
    return [points[int(i * step) % n] for i in range(k)]


def _fit_arc_through_three_points(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    tol: float = 0.002,
) -> tuple[float, float, float, float, float, float] | None:
    """If the three points are nearly cocircular and p1 lies between p0 and p2 along the arc, return (xs,ys,xm,ym,xe,ye). Else None."""
    x0, y0 = p0
    x1, y1 = p1
    x2, y2 = p2
    circ = _circle_through_three_points(x0, y0, x1, y1, x2, y2)
    if circ is None:
        return None
    cx, cy, r = circ
    if r < 1e-9:
        return None
    d1 = abs(math.hypot(x1 - cx, y1 - cy) - r)
    if d1 > tol:
        return None
    theta0 = math.atan2(y0 - cy, x0 - cx)
    theta1 = math.atan2(y1 - cy, x1 - cx)
    theta2 = math.atan2(y2 - cy, x2 - cx)
    # Check p1 is between p0 and p2 along the arc (sweep direction)
    d01 = (theta1 - theta0 + 3 * math.pi) % (2 * math.pi) - math.pi
    d12 = (theta2 - theta1 + 3 * math.pi) % (2 * math.pi) - math.pi
    if abs(d01 + d12) > math.pi:
        return None
    return (x0, y0, x1, y1, x2, y2)


def _interpolated_points_to_primitives(
    points: list[tuple[float, float]],
    arc_fit_tol: float = 0.002,
) -> list[tuple[str, ...]]:
    """Convert closed polyline to list of ('segment', ...) or ('arc', ...). Fits arcs through consecutive triples when cocircular."""
    n = len(points)
    if n < 2:
        return []
    out: list[tuple[str, ...]] = []
    covered = 0
    i = 0
    while covered < n:
        j = (i + 1) % n
        k = (i + 2) % n
        if covered == n - 1:
            out.append(("segment", points[i][0], points[i][1], points[j][0], points[j][1]))
            break
        arc_pts = _fit_arc_through_three_points(points[i], points[j], points[k], tol=arc_fit_tol)
        if arc_pts is not None:
            out.append(("arc", arc_pts[0], arc_pts[1], arc_pts[2], arc_pts[3], arc_pts[4], arc_pts[5]))
            covered += 2
            i = (i + 2) % n
        else:
            out.append(("segment", points[i][0], points[i][1], points[j][0], points[j][1]))
            covered += 1
            i = (i + 1) % n
    return out


def _arc_ratio_from_primitives(prims: list[tuple[str, ...]]) -> float:
    """Fraction of primitives that are arcs (0 = all segments, 1 = all arcs)."""
    if not prims:
        return 0.0
    n_arcs = sum(1 for p in prims if p[0] == "arc")
    return n_arcs / len(prims)


def _arc_fit_tol_to_match_arc_ratio(
    points: list[tuple[float, float]],
    target_arc_ratio: float,
    tol_min: float = 0.0005,
    tol_max: float = 0.1,
    n_tries: int = 15,
) -> float:
    """Find arc_fit_tol so that _interpolated_points_to_primitives(points, tol) has arc ratio close to target.
    Higher tol -> fewer arcs (more angular). Used so morph targets B's curvature at t=1 (no overshoot).
    """
    if len(points) < 3 or target_arc_ratio < 0 or target_arc_ratio > 1:
        return 0.002
    best_tol = 0.002
    best_err = 1.0
    for k in range(n_tries + 1):
        t = k / n_tries if n_tries > 0 else 1.0
        tol = tol_min + (tol_max - tol_min) * t
        prims = _interpolated_points_to_primitives(points, arc_fit_tol=tol)
        ratio = _arc_ratio_from_primitives(prims)
        err = abs(ratio - target_arc_ratio)
        if err < best_err:
            best_err = err
            best_tol = tol
    return best_tol


def _morph_easing(t: float, mode: str = "linear") -> float:
    """Map linear t in [0,1] to an eased t for a more natural morph. t=0 -> 0, t=1 -> 1."""
    t = max(0.0, min(1.0, t))
    if mode == "linear":
        return t
    if mode == "smoothstep":
        return t * t * (3.0 - 2.0 * t)
    if mode == "smootherstep":
        return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)
    if mode == "ease_in_out_sine":
        return 0.5 * (1.0 - cos(math.pi * t))
    return t


def _pattern_primitives_from_svg(
    svg_path: Path,
    panel_ox_mm: float,
    panel_oy_mm: float,
    panel_w_mm: float,
    panel_h_mm: float,
    scale: float = 1.0,
) -> tuple[list[tuple[str, ...]], float, float]:
    """Load pattern SVG, parse to line/arc primitives, tile and clip. Returns (primitives, tile_w, tile_h).
    Primitives are ('segment', x1,y1,x2,y2) or ('arc', xs,ys, xm,ym, xe,ye) in panel mm.
    Arcs are kept when fully inside clip rect; otherwise approximated as segments.
    """
    if not svg_path.exists():
        return [], 0.0, 0.0
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ns = "{http://www.w3.org/2000/svg}"
    w_attr = root.get("width", "0").replace("px", "").strip()
    h_attr = root.get("height", "0").replace("px", "").strip()
    tile_w = float(w_attr) if w_attr else 23.07
    tile_h = float(h_attr) if h_attr else 40.0
    path_d = None
    for elem in root.iter(f"{ns}path"):
        path_d = elem.get("d")
        if path_d:
            break
    if not path_d:
        return [], tile_w, tile_h
    try:
        raw = _parse_svg_path_to_primitives(path_d)
    except (ValueError, ZeroDivisionError):
        raw = [("segment", s[0], s[1], s[2], s[3]) for s in _parse_svg_path_to_segments(path_d)]
    effective_tile_w = tile_w * scale
    effective_tile_h = tile_h * scale
    rx_min = panel_ox_mm
    ry_min = panel_oy_mm
    rx_max = panel_ox_mm + panel_w_mm
    ry_max = panel_oy_mm + panel_h_mm
    out: list[tuple[str, ...]] = []
    ni = max(1, math.ceil(panel_w_mm / effective_tile_w) + 1)
    nj = max(1, math.ceil(panel_h_mm / effective_tile_h) + 1)
    for ji in range(nj):
        for ii in range(ni):
            tx = panel_ox_mm + ii * effective_tile_w
            ty = panel_oy_mm + ji * effective_tile_h
            for prim in raw:
                if prim[0] == "segment":
                    x1, y1, x2, y2 = prim[1], prim[2], prim[3], prim[4]
                    seg = (tx + x1 * scale, ty + y1 * scale, tx + x2 * scale, ty + y2 * scale)
                    for clipped in _clip_segment_to_rect(
                        seg[0], seg[1], seg[2], seg[3], rx_min, ry_min, rx_max, ry_max
                    ):
                        out.append(("segment", clipped[0], clipped[1], clipped[2], clipped[3]))
                else:
                    xs, ys, xm, ym, xe, ye = prim[1], prim[2], prim[3], prim[4], prim[5], prim[6]
                    arc_pts = (
                        (tx + xs * scale, ty + ys * scale),
                        (tx + xm * scale, ty + ym * scale),
                        (tx + xe * scale, ty + ye * scale),
                    )
                    ax_min = min(p[0] for p in arc_pts)
                    ax_max = max(p[0] for p in arc_pts)
                    ay_min = min(p[1] for p in arc_pts)
                    ay_max = max(p[1] for p in arc_pts)
                    if (
                        ax_max >= rx_min and ax_min <= rx_max
                        and ay_max >= ry_min and ay_min <= ry_max
                    ):
                        out.append(("arc", arc_pts[0][0], arc_pts[0][1], arc_pts[1][0], arc_pts[1][1], arc_pts[2][0], arc_pts[2][1]))
                    else:
                        for (xa, ya), (xb, yb) in [
                            ((arc_pts[0][0], arc_pts[0][1]), (arc_pts[1][0], arc_pts[1][1])),
                            ((arc_pts[1][0], arc_pts[1][1]), (arc_pts[2][0], arc_pts[2][1])),
                        ]:
                            for clipped in _clip_segment_to_rect(
                                xa, ya, xb, yb, rx_min, ry_min, rx_max, ry_max
                            ):
                                out.append(("segment", clipped[0], clipped[1], clipped[2], clipped[3]))
    return out, effective_tile_w, effective_tile_h


def _clip_primitive_to_rect(
    prim: tuple[str, ...],
    rx_min: float,
    ry_min: float,
    rx_max: float,
    ry_max: float,
) -> list[tuple[str, ...]]:
    """Clip a single primitive to rect. Returns list of ('segment', x1,y1,x2,y2) or one ('arc', ...).
    Segments are clipped to the rect. Arcs that overlap the rect are emitted in full (no physical
    clipping of arcs); arcs that do not overlap are dropped.
    """
    if prim[0] == "segment":
        x1, y1, x2, y2 = prim[1], prim[2], prim[3], prim[4]
        return [
            ("segment", c[0], c[1], c[2], c[3])
            for c in _clip_segment_to_rect(x1, y1, x2, y2, rx_min, ry_min, rx_max, ry_max)
        ]
    # arc: keep if bbox overlaps rect (so curved traces render; arc may extend outside zone)
    xs, ys, xm, ym, xe, ye = prim[1], prim[2], prim[3], prim[4], prim[5], prim[6]
    ax_min = min(xs, xm, xe)
    ax_max = max(xs, xm, xe)
    ay_min = min(ys, ym, ye)
    ay_max = max(ys, ym, ye)
    if ax_max >= rx_min and ax_min <= rx_max and ay_max >= ry_min and ay_min <= ry_max:
        return [prim]
    return []


def _get_primitive_endpoints(prim: tuple[str, ...]) -> list[tuple[float, float]]:
    """Return the two endpoints of a segment or arc (start and end)."""
    if prim[0] == "segment":
        return [(prim[1], prim[2]), (prim[3], prim[4])]
    if prim[0] == "arc":
        return [(prim[1], prim[2]), (prim[5], prim[6])]
    return []


def _get_primitive_endpoints_with_side(
    prim: tuple[str, ...],
) -> list[tuple[float, float, bool]]:
    """Return (x, y, is_right_end) for each endpoint. is_right_end = this endpoint has the larger x (eastern end).
    For vertical segments (same x), first endpoint is left, second is right, so each primitive contributes one to each side.
    """
    if prim[0] == "segment":
        x1, y1, x2, y2 = prim[1], prim[2], prim[3], prim[4]
        if x1 > x2:
            return [(x1, y1, True), (x2, y2, False)]
        if x1 < x2:
            return [(x1, y1, False), (x2, y2, True)]
        return [(x1, y1, False), (x2, y2, True)]  # vertical: first=left, second=right
    if prim[0] == "arc":
        xs, ys, xe, ye = prim[1], prim[2], prim[5], prim[6]
        if xs > xe:
            return [(xs, ys, True), (xe, ye, False)]
        if xs < xe:
            return [(xs, ys, False), (xe, ye, True)]
        return [(xs, ys, False), (xe, ye, True)]
    return []


def _connect_boundary_endpoints(
    primitives: list[tuple[str, ...]],
    x_morph_start: float,
    x_left: float,
    x_right: float,
    x_morph_end: float,
    ry_min: float,
    ry_max: float,
) -> list[tuple[str, ...]]:
    """Connect discontinuous pattern lines at A|morph and morph|B boundaries.
    Each discontinuity gets exactly one segment: from the centroid of left-side
    endpoints to the centroid of right-side endpoints (no vertical; end at +x from origin).
    """
    tol_x = PATTERN_BOUNDARY_CONNECT_TOL_X
    out: list[tuple[str, ...]] = []

    def on_a_band(x: float) -> bool:
        return x >= x_morph_start - tol_x and x <= x_morph_start + tol_x

    def on_morph_left_band(x: float) -> bool:
        return x >= x_left - tol_x and x <= x_left + tol_x

    def on_morph_right_band(x: float) -> bool:
        return x >= x_right - tol_x and x <= x_right + tol_x

    def on_b_band(x: float) -> bool:
        return x >= x_morph_end - tol_x and x <= x_morph_end + tol_x

    def collect_endpoints(zone: str) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for prim in primitives:
            for (x, y, is_right) in _get_primitive_endpoints_with_side(prim):
                if not (ry_min <= y <= ry_max):
                    continue
                if zone == "A":
                    if on_a_band(x) and is_right:
                        pts.append((x, y))
                elif zone == "morph_left":
                    if on_morph_left_band(x) and not is_right:
                        pts.append((x, y))
                elif zone == "morph_right":
                    if on_morph_right_band(x) and is_right:
                        pts.append((x, y))
                elif zone == "morph_rightmost":
                    # Rightmost edge of morph zone (same x as B left): morph-side endpoints
                    if on_b_band(x) and is_right:
                        pts.append((x, y))
                elif zone == "B":
                    if on_b_band(x) and not is_right:
                        pts.append((x, y))
        return pts

    def connect_one_segment(
        left_pts: list[tuple[float, float]],
        right_pts: list[tuple[float, float]],
    ) -> None:
        """Emit a single segment per boundary: centroid of left -> centroid of right.
        No vertical: end point at positive x from origin.
        """
        if not left_pts or not right_pts:
            return
        min_dx = PATTERN_CONNECT_MIN_DX_MM
        lx = sum(p[0] for p in left_pts) / len(left_pts)
        ly = sum(p[1] for p in left_pts) / len(left_pts)
        rx = sum(p[0] for p in right_pts) / len(right_pts)
        ry = sum(p[1] for p in right_pts) / len(right_pts)
        end_x = rx if rx > lx else lx + min_dx
        out.append(("segment", lx, ly, end_x, ry))

    # Left boundary: one segment A right edge -> morph left edge
    a_right = collect_endpoints("A")
    morph_left = collect_endpoints("morph_left")
    connect_one_segment(a_right, morph_left)

    # Right boundary: one segment morph right edge -> B left edge
    morph_r = collect_endpoints("morph_right")
    b_left = collect_endpoints("B")
    connect_one_segment(morph_r, b_left)

    # Rightmost edge of morph zone (at x_morph_end): connect morph-side endpoints to B left edge
    morph_rightmost = collect_endpoints("morph_rightmost")
    connect_one_segment(morph_rightmost, b_left)

    return out


def _pattern_segments_from_svg(
    svg_path: Path,
    panel_ox_mm: float,
    panel_oy_mm: float,
    panel_w_mm: float,
    panel_h_mm: float,
    scale: float = 1.0,
) -> tuple[list[tuple[float, float, float, float]], float, float]:
    """Load pattern SVG, parse path, tile and clip to panel. Return (segments in KiCad mm, tile_w, tile_h).
    scale: 1.0 = one tile per SVG size; 0.5 = half-size pattern (twice as many tiles).
    """
    if not svg_path.exists():
        return [], 0.0, 0.0
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ns = "{http://www.w3.org/2000/svg}"
    w_attr = root.get("width", "0").replace("px", "").strip()
    h_attr = root.get("height", "0").replace("px", "").strip()
    tile_w = float(w_attr) if w_attr else 23.07
    tile_h = float(h_attr) if h_attr else 40.0
    path_d = None
    for elem in root.iter(f"{ns}path"):
        path_d = elem.get("d")
        if path_d:
            break
    if not path_d:
        return [], tile_w, tile_h
    raw = _parse_svg_path_to_segments(path_d)
    effective_tile_w = tile_w * scale
    effective_tile_h = tile_h * scale
    rx_min = panel_ox_mm
    ry_min = panel_oy_mm
    rx_max = panel_ox_mm + panel_w_mm
    ry_max = panel_oy_mm + panel_h_mm
    out: list[tuple[float, float, float, float]] = []
    ni = max(1, math.ceil(panel_w_mm / effective_tile_w) + 1)
    nj = max(1, math.ceil(panel_h_mm / effective_tile_h) + 1)
    for ji in range(nj):
        for ii in range(ni):
            tx = panel_ox_mm + ii * effective_tile_w
            ty = panel_oy_mm + ji * effective_tile_h
            for (x1, y1, x2, y2) in raw:
                seg = (
                    tx + x1 * scale,
                    ty + y1 * scale,
                    tx + x2 * scale,
                    ty + y2 * scale,
                )
                for clipped in _clip_segment_to_rect(
                    seg[0], seg[1], seg[2], seg[3], rx_min, ry_min, rx_max, ry_max
                ):
                    out.append(clipped)
    return out, effective_tile_w, effective_tile_h


def _pattern_segments_three_zone(
    panel_ox_mm: float,
    panel_oy_mm: float,
    panel_w_mm: float,
    panel_h_mm: float,
    scale_a: float = 1.0,
    scale_b: float = 1.0,
) -> list[tuple[str, ...]]:
    """Generate pattern primitives: left = pure A (pattern.svg); morph zone = A→B; right strip = pure B (pattern_c.svg, arcs preserved).
    Returns list of ('segment', x1,y1,x2,y2) or ('arc', xs,ys,xm,ym,xe,ye) in panel mm.
    """
    # Left edge of morph = midpoint of panel (transition to B begins here)
    x_morph_start = panel_ox_mm + PATTERN_ZONE_A_WIDTH * panel_w_mm
    x_morph_end = panel_ox_mm + (1.0 - PATTERN_ZONE_B_WIDTH) * panel_w_mm
    rx_full_min = panel_ox_mm
    ry_min = panel_oy_mm
    rx_full_max = panel_ox_mm + panel_w_mm
    ry_max = panel_oy_mm + panel_h_mm

    out: list[tuple[str, ...]] = []

    # Left zone: pattern A only (PATTERN_SCALE_A), clipped to [panel_ox, x_morph_start]
    segs_a, _, _ = _pattern_segments_from_svg(
        PATTERN_SVG, panel_ox_mm, panel_oy_mm, panel_w_mm, panel_h_mm, scale_a
    )
    for (x1, y1, x2, y2) in segs_a:
        for clipped in _clip_segment_to_rect(
            x1, y1, x2, y2, rx_full_min, ry_min, x_morph_start, ry_max
        ):
            out.append(("segment", clipped[0], clipped[1], clipped[2], clipped[3]))

    # Right zone: pattern B with arcs (PATTERN_B_SVG), primitives clipped to [x_morph_end, rx_full_max]
    if PATTERN_ZONE_B_WIDTH > 0:
        prims_b, _, _ = _pattern_primitives_from_svg(
            PATTERN_B_SVG, panel_ox_mm, panel_oy_mm, panel_w_mm, panel_h_mm, scale_b
        )
        for prim in prims_b:
            out.extend(_clip_primitive_to_rect(prim, x_morph_end, ry_min, rx_full_max, ry_max))

    # Morph zone [x_morph_start, x_morph_end]: A→B with curved traces and continuous column sizing
    # Apply insets so morph does not overlap zone A or B (strict boundaries)
    inset_a = PATTERN_MORPH_INSET_A_MM
    inset_b = PATTERN_MORPH_INSET_B_MM
    x_left = x_morph_start + inset_a
    x_right = x_morph_end - inset_b
    if x_left >= x_right:
        x_left = x_morph_start
        x_right = x_morph_end
    points_a, tile_w_a, tile_h_a = _load_pattern_normalized_resampled(
        PATTERN_SVG, PATTERN_MORPH_RESAMPLE_POINTS
    )
    points_b, tile_w_b, tile_h_b = _load_pattern_normalized_resampled(
        PATTERN_B_SVG, PATTERN_MORPH_RESAMPLE_POINTS
    )
    if not points_a or not points_b:
        return out
    # Same point count for 1:1 correspondence (continuous lines when tiled)
    n_pts = min(len(points_a), len(points_b))
    if n_pts < 3:
        return out
    points_a = points_a[:n_pts]
    points_b = points_b[:n_pts]
    # Same winding so morph doesn't flip; align start index so corresponding points match phase
    points_a, points_b = _normalize_morph_winding(points_a, points_b)
    points_a, points_b = _align_morph_starts(points_a, points_b)
    # Target B's curvature at end of morph (no overshoot): blend arc_fit_tol from A's to B's level
    arc_ratio_a = _load_pattern_arc_ratio(PATTERN_SVG)
    arc_ratio_b = _load_pattern_arc_ratio(PATTERN_B_SVG)
    tol_a = _arc_fit_tol_to_match_arc_ratio(points_a, arc_ratio_a)
    tol_b = _arc_fit_tol_to_match_arc_ratio(points_b, arc_ratio_b)
    n_cols = PATTERN_MORPH_NUM_COLUMNS
    middle_w = x_right - x_left
    # Per-column width so t=0 matches left zone tile width and t=1 matches right zone (no discontinuity)
    tile_wa = tile_w_a * scale_a
    tile_wb = tile_w_b * scale_b
    raw_widths = [
        (1.0 - _morph_easing((i + 0.5) / n_cols, PATTERN_MORPH_EASING)) * tile_wa
        + _morph_easing((i + 0.5) / n_cols, PATTERN_MORPH_EASING) * tile_wb
        for i in range(n_cols)
    ]
    sum_raw = sum(raw_widths)
    col_widths = [middle_w * w / sum_raw for w in raw_widths] if sum_raw > 0 else [middle_w / n_cols] * n_cols
    x_offsets: list[float] = []
    acc = 0.0
    for w in col_widths:
        x_offsets.append(x_left + acc)
        acc += w
    n_rows = max(1, math.ceil(panel_h_mm / (40.0 * min(scale_a, scale_b))) + 1)
    for i in range(n_cols):
        t_linear = (i + 0.5) / n_cols
        t = _morph_easing(t_linear, PATTERN_MORPH_EASING)
        interp = _interpolate_points(points_a, points_b, t)
        # Clamp morph shape complexity to match simpler A/B density
        if PATTERN_MORPH_COMPLEXITY < 1.0:
            target_pts = max(3, int(round(len(interp) * PATTERN_MORPH_COMPLEXITY)))
            interp = _subsample_polyline_closed(interp, target_pts)
        # Blend arc_fit_tol so morph ends at B's curvature (more angular B -> fewer arcs at t=1)
        arc_fit_tol = (1.0 - t) * tol_a + t * tol_b
        prims = _interpolated_points_to_primitives(interp, arc_fit_tol=arc_fit_tol)
        col_w = col_widths[i]
        x0 = x_offsets[i]
        tile_h = 40.0 * ((1.0 - t) * scale_a + t * scale_b)
        for j in range(n_rows):
            ty = panel_oy_mm + j * tile_h
            for prim in prims:
                if prim[0] == "segment":
                    x1_ = x0 + prim[1] * col_w
                    y1_ = ty + prim[2] * tile_h
                    x2_ = x0 + prim[3] * col_w
                    y2_ = ty + prim[4] * tile_h
                    for clipped in _clip_segment_to_rect(
                        x1_, y1_, x2_, y2_, x_left, ry_min, x_right, ry_max
                    ):
                        out.append(("segment", clipped[0], clipped[1], clipped[2], clipped[3]))
                else:
                    xs_ = x0 + prim[1] * col_w
                    ys_ = ty + prim[2] * tile_h
                    xm_ = x0 + prim[3] * col_w
                    ym_ = ty + prim[4] * tile_h
                    xe_ = x0 + prim[5] * col_w
                    ye_ = ty + prim[6] * tile_h
                    arc_prim = ("arc", xs_, ys_, xm_, ym_, xe_, ye_)
                    clipped_list = _clip_primitive_to_rect(arc_prim, x_left, ry_min, x_right, ry_max)
                    out.extend(clipped_list)
    # Boundary connection: link each discontinuity to the closest neighbor to its right (segment)
    connectors = _connect_boundary_endpoints(
        out, x_morph_start, x_left, x_right, x_morph_end, ry_min, ry_max,
    )
    out.extend(connectors)
    return out


def _build_reference_geometry(
    holes: list[tuple[float, float, str]],
    sd_slot: tuple[float, float, float, float] | None,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float, float]]]:
    """Build expected circles (x, y, r_panel) and rects (x, y, w, h) for validation.
    Circles exclude mount (screw) positions; those are represented by rects when USE_DRILL_SLOTS else Edge.Cuts rects.
    """
    circles: list[tuple[float, float, float]] = []
    for (x, y, family) in holes:
        if family == FAMILY_MOUNT:
            continue
        d = _panel_cutout_diameter_mm(family)
        circles.append((x, y, d / 2.0))
    rects: list[tuple[float, float, float, float]] = list(_screw_slot_rects())
    if sd_slot is not None:
        rects.append(sd_slot)
    return circles, rects


def _write_hardware_centers_svg(
    out_path: Path,
    holes: list[tuple[float, float, str]],
    sd_slot: tuple[float, float, float, float] | None,
) -> None:
    """Write hardware-centers-kicad.svg (panel cutout circles + screw/SD rects)."""
    circles, rects = _build_reference_geometry(holes, sd_slot)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PANEL_WIDTH_MM}mm" height="{PANEL_HEIGHT_MM}mm" '
        f'viewBox="0 0 {PANEL_WIDTH_MM} {PANEL_HEIGHT_MM}">',
        f'  <rect x="0" y="0" width="{PANEL_WIDTH_MM:.3f}" height="{PANEL_HEIGHT_MM:.3f}" '
        'fill="none" stroke="#808080" stroke-width="0.1" />',
        "  <!-- Drill circles (blue) and cutout rects -->",
    ]
    for (x, y, r) in circles:
        lines.append(
            f'  <circle cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" fill="none" '
            'stroke="#0000ff" stroke-width="0.2" />'
        )
    for (sx, sy, w, h) in rects:
        lines.append(
            f'  <rect x="{sx:.3f}" y="{sy:.3f}" width="{w:.3f}" height="{h:.3f}" '
            'fill="none" stroke="#0000ff" stroke-width="0.2" />'
        )
    lines.append("</svg>")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _silkscreen_bboxes_at_size(
    holes: list[tuple[float, float, str]],
    body_size_mm: float,
    include_titles: bool = True,
) -> list[tuple[float, float, float, float]]:
    """Return list of (x_min, y_min, x_max, y_max) in panel mm for title + component labels at given size.
    If include_titles is False, only component label bboxes are returned (for fitting).
    Title bboxes use body_size_mm and are centered vertically on TITLE_Y_*_MM (so mask aligns with text).
    """
    m = SILK_MARGIN_MM
    bboxes: list[tuple[float, float, float, float]] = []
    if include_titles:
        for (text, y_panel) in [(TITLE_TOP, TITLE_Y_TOP_MM), (TITLE_BOTTOM, TITLE_Y_BOTTOM_MM)]:
            w = _estimate_text_width_mm(text, body_size_mm)
            x_left = TITLE_X_MM - w / 2.0
            # Center bbox vertically on y_panel so mask rect aligns with text
            half_h = body_size_mm / 2.0
            bboxes.append((x_left - m, y_panel - half_h - m, x_left + w + m, y_panel + half_h + m))
    label_list = _match_labels_to_holes(holes)
    for (drill_x, drill_y, family, text, _) in label_list:
        r_mm = _panel_cutout_diameter_mm(family) / 2.0
        w = _estimate_text_width_mm(text, body_size_mm)
        y_bottom = drill_y - r_mm - SILK_LABEL_OFFSET_BELOW_MM - body_size_mm
        x_left = drill_x - w / 2.0
        bboxes.append((x_left - m, y_bottom - m, x_left + w + m, y_bottom + body_size_mm + m))
    return bboxes


def _label_mask_rects_board_mm(
    holes: list[tuple[float, float, str]],
    ox_mm: float,
    oy_mm: float,
) -> list[tuple[float, float, float, float]]:
    """Return (x_min, y_min, x_max, y_max) in board mm for each label 'text box' (for solder mask behind text).
    Uses fitted silkscreen size; vertical padding MASK_LABEL_PAD_MM. Horizontal: text bounds are bbox ± SILK_MARGIN_MM;
    we expand by MASK_LABEL_WIDTH_EXTRA_FRAC of that width (rendered text often wider than estimate) then extend
    MASK_LABEL_EXTEND_H_MM past that, so mask clears the leftmost/rightmost letters (e.g. M and P in MAX COMP).
    The first two rects (title top + title bottom) are merged into one so KiCad zone fill does not remove mask in their intersection.
    """
    size_mm = _silkscreen_fit_size(holes)
    pad_v = MASK_LABEL_PAD_MM
    m = SILK_MARGIN_MM
    ext_h = MASK_LABEL_EXTEND_H_MM
    extra_frac = MASK_LABEL_WIDTH_EXTRA_FRAC
    bboxes = _silkscreen_bboxes_at_size(holes, size_mm, include_titles=True)
    result = []
    for (x_min, y_min, x_max, y_max) in bboxes:
        text_width = (x_max - x_min) - 2 * m
        # Extra expansion so mask extends past actual glyphs when width estimate is low
        half_extra = 0.5 * text_width * extra_frac
        mask_x_min = x_min + m - half_extra - ext_h + ox_mm
        mask_x_max = x_max - m + half_extra + ext_h + ox_mm
        result.append((mask_x_min, y_min - pad_v + oy_mm, mask_x_max, y_max + pad_v + oy_mm))
    # Merge the two title mask rects into one so KiCad fill does not remove mask in the intersection
    if len(result) >= 2:
        a, b = result[0], result[1]
        merged = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
        result = [merged] + result[2:]
    # Decompose any other intersecting pairs (e.g. MAX COMP and PITCH LOCK) into non-overlapping
    # rects so zone fill does not remove mask in intersections; same padding effect as if intersection were mask.
    changed = True
    while changed:
        changed = False
        for i in range(len(result)):
            for j in range(i + 1, len(result)):
                if _bboxes_overlap(result[i], result[j]):
                    decomposed = _decompose_union_two_rects(result[i], result[j])
                    result = result[:i] + decomposed + result[i + 1 : j] + result[j + 1 :]
                    changed = True
                    break
            if changed:
                break
    return result


def _silkscreen_shape_polygons_board_mm(ox_mm: float, oy_mm: float) -> list[list[tuple[float, float]]]:
    """Return polygon points in board mm for each silkscreen shape that should have solder mask underneath.
    Includes the 5 output jack rounded rectangles, expanded by MASK_LABEL_PAD_MM on all sides so the mask
    has that padding around the silkscreen shape.
    """
    pad = MASK_LABEL_PAD_MM
    size_with_pad = SILK_OUTPUT_JACK_BOX_SIZE_MM + 2.0 * pad
    half = size_with_pad / 2.0
    radius = min(SILK_OUTPUT_JACK_BOX_RADIUS_MM, half - 0.01)
    polygons: list[list[tuple[float, float]]] = []
    for (cx, cy) in SILK_OUTPUT_JACK_BOXES_MM:
        x1 = cx - half + ox_mm
        y1 = cy - half + oy_mm
        x2 = cx + half + ox_mm
        y2 = cy + half + oy_mm
        pts = _rounded_rect_polygon_pts(x1, y1, x2, y2, radius)
        polygons.append(pts)
    return polygons


def _solder_mask_zone_lines(
    ox_mm: float,
    oy_mm: float,
    w_mm: float,
    h_mm: float,
    label_rects_board_mm: list[tuple[float, float, float, float]],
    silkscreen_polygons_board_mm: list[list[tuple[float, float]]],
) -> list[str]:
    """Return one (zone ...) s-expr for F.Mask: board outline with holes at labels and shapes (inverted).

    Single zone with first polygon = full board rectangle, remaining polygons = holes (label rects +
    output jack shapes). When KiCad fills this zone, the fill is board minus holes, so mask covers
    the background and label/shape areas stay exposed (solder mask opening). Use Fill in KiCad to
    update the zone fill.
    """
    board_pts = (
        f"(xy {ox_mm:.4f} {oy_mm:.4f}) (xy {ox_mm + w_mm:.4f} {oy_mm:.4f}) "
        f"(xy {ox_mm + w_mm:.4f} {oy_mm + h_mm:.4f}) (xy {ox_mm:.4f} {oy_mm + h_mm:.4f})"
    )
    lines = [
        '  (zone (net 0) (net_name "") (layer "F.Mask") (tstamp ' + _uuid() + ') (hatch edge 0.508)',
        '    (connect_pads no) (min_thickness 0.25)',
        '    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))',
        '    (polygon (pts ' + board_pts + '))',
    ]
    for (x_min, y_min, x_max, y_max) in label_rects_board_mm:
        hole_pts = (
            f"(xy {x_min:.4f} {y_min:.4f}) (xy {x_max:.4f} {y_min:.4f}) "
            f"(xy {x_max:.4f} {y_max:.4f}) (xy {x_min:.4f} {y_max:.4f})"
        )
        lines.append('    (polygon (pts ' + hole_pts + '))')
    for polygon_pts in silkscreen_polygons_board_mm:
        pts_str = " ".join(f"(xy {x:.4f} {y:.4f})" for (x, y) in polygon_pts)
        lines.append('    (polygon (pts ' + pts_str + '))')
    lines.append('  )')
    return lines


def _bboxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def _decompose_union_two_rects(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    """Decompose the union of two axis-aligned rects into non-overlapping axis-aligned rects.
    Returns a list of rects (x_min, y_min, x_max, y_max) whose union is A∪B with no overlaps.
    So KiCad zone fill treats the union as one hole without removing mask in the intersection.
    """
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    # No overlap: return both
    if ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0:
        return [a, b]
    # Build grid from both rects' edges
    xs = sorted(set([ax0, ax1, bx0, bx1]))
    ys = sorted(set([ay0, ay1, by0, by1]))
    out: list[tuple[float, float, float, float]] = []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx0, cx1 = xs[i], xs[i + 1]
            cy0, cy1 = ys[j], ys[j + 1]
            # Cell in A?
            in_a = ax0 <= cx0 and cx1 <= ax1 and ay0 <= cy0 and cy1 <= ay1
            # Cell in B?
            in_b = bx0 <= cx0 and cx1 <= bx1 and by0 <= cy0 and cy1 <= by1
            if in_a or in_b:
                out.append((cx0, cy0, cx1, cy1))
    return out if out else [a, b]


def _silkscreen_fit_size(holes: list[tuple[float, float, str]]) -> float:
    """Largest body_size_mm such that all component label bboxes fit in panel with no overlap.
    Titles (TITLE_TOP, TITLE_BOTTOM) are not included in the fit; they use the same size.
    """
    for size_mm in [
        x / 100.0
        for x in range(
            int(SILK_BODY_SIZE_MAX_MM * 100),
            int((SILK_BODY_SIZE_MIN_MM - 0.05) * 100),
            -5,
        )
    ]:
        bboxes = _silkscreen_bboxes_at_size(holes, size_mm, include_titles=False)
        if any(
            b[0] < SILK_MARGIN_MM
            or b[1] < SILK_MARGIN_MM
            or b[2] > PANEL_WIDTH_MM - SILK_MARGIN_MM
            or b[3] > PANEL_HEIGHT_MM - SILK_MARGIN_MM
            for b in bboxes
        ):
            continue
        if any(
            _bboxes_overlap(bboxes[i], bboxes[j])
            for i in range(len(bboxes))
            for j in range(i + 1, len(bboxes))
        ):
            continue
        return size_mm
    return SILK_BODY_SIZE_MIN_MM


def _rounded_rect_polygon_pts(
    x1: float, y1: float, x2: float, y2: float, r_mm: float, num_pts_per_arc: int = 6
) -> list[tuple[float, float]]:
    """Return polygon points for a rounded rectangle (clockwise, for gr_poly fill).
    (x1,y1) = top-left, (x2,y2) = bottom-right; r_mm = corner radius. Y increases downward.
    """
    if r_mm <= 0 or r_mm >= (min(x2 - x1, y2 - y1) / 2.0):
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    pts: list[tuple[float, float]] = []
    # Top edge
    pts.append((x1 + r_mm, y1))
    pts.append((x2 - r_mm, y1))
    # Top-right arc: center (x2-r, y1+r), from 270° to 360°
    for i in range(1, num_pts_per_arc):
        ang = radians(270 + 90 * i / num_pts_per_arc)
        pts.append((x2 - r_mm + r_mm * cos(ang), y1 + r_mm + r_mm * sin(ang)))
    pts.append((x2, y1 + r_mm))
    # Right edge
    pts.append((x2, y2 - r_mm))
    # Bottom-right arc: center (x2-r, y2-r), 0° to 90°
    for i in range(1, num_pts_per_arc):
        ang = radians(0 + 90 * i / num_pts_per_arc)
        pts.append((x2 - r_mm + r_mm * cos(ang), y2 - r_mm + r_mm * sin(ang)))
    pts.append((x2 - r_mm, y2))
    # Bottom edge
    pts.append((x1 + r_mm, y2))
    # Bottom-left arc: center (x1+r, y2-r), 90° to 180°
    for i in range(1, num_pts_per_arc):
        ang = radians(90 + 90 * i / num_pts_per_arc)
        pts.append((x1 + r_mm + r_mm * cos(ang), y2 - r_mm + r_mm * sin(ang)))
    pts.append((x1, y2 - r_mm))
    # Left edge
    pts.append((x1, y1 + r_mm))
    # Top-left arc: center (x1+r, y1+r), 180° to 270°
    for i in range(1, num_pts_per_arc):
        ang = radians(180 + 90 * i / num_pts_per_arc)
        pts.append((x1 + r_mm + r_mm * cos(ang), y1 + r_mm + r_mm * sin(ang)))
    return pts


def _silkscreen_output_jack_rect_lines(ox_mm: float, oy_mm: float) -> list[str]:
    """Return F.SilkS lines for the five output jack boxes (gr_poly when radius > 0, else gr_rect).
    Same positions as _deprecated_generate_panel.py; corner radius from SILK_OUTPUT_JACK_BOX_RADIUS_MM.
    """
    half = SILK_OUTPUT_JACK_BOX_SIZE_MM / 2.0
    radius = min(SILK_OUTPUT_JACK_BOX_RADIUS_MM, half - 0.01)  # cap to valid range
    lines: list[str] = []
    for (cx, cy) in SILK_OUTPUT_JACK_BOXES_MM:
        x1 = cx - half + ox_mm
        y1 = cy - half + oy_mm
        x2 = cx + half + ox_mm
        y2 = cy + half + oy_mm
        if radius <= 0:
            lines.append(
                f'  (gr_rect (start {x1:.4f} {y1:.4f}) (end {x2:.4f} {y2:.4f}) '
                f'(layer F.SilkS) (width {SILK_OUTPUT_JACK_BOX_STROKE_MM:.4f}) (fill yes) (tstamp {_uuid()}))'
            )
        else:
            pts = _rounded_rect_polygon_pts(x1, y1, x2, y2, radius)
            pts_str = " ".join(f"(xy {x:.4f} {y:.4f})" for (x, y) in pts)
            lines.append(
                f'  (gr_poly (pts {pts_str}) (layer F.SilkS) '
                f'(width {SILK_OUTPUT_JACK_BOX_STROKE_MM:.4f}) (fill yes) (tstamp {_uuid()}))'
            )
    return lines


def _silkscreen_gr_text_lines(
    ox_mm: float,
    oy_mm: float,
    holes: list[tuple[float, float, str]],
) -> list[str]:
    """Return F.SilkS gr_text lines: title centered at top, each label centered under its drill hole.
    Font size is chosen so all labels fit without overlap or extending past the PCB edge.
    """
    lines: list[str] = []
    size_mm = _silkscreen_fit_size(holes)

    def gr_text_at(x_panel: float, y_panel: float, text: str, font_size_mm: float, italic: bool | None = None) -> str:
        x = x_panel + ox_mm
        y = y_panel + oy_mm
        th = max(0.08, font_size_mm * 0.07)
        font_parts = []
        if SILK_FONT_FACE:
            font_parts.append(f'(face "{SILK_FONT_FACE}")')
        font_parts.append(f'(size {font_size_mm:.2f} {font_size_mm:.2f})')
        font_parts.append(f'(thickness {th:.3f})')
        if SILK_FONT_BOLD:
            font_parts.append("bold")
        use_italic = italic if italic is not None else SILK_FONT_ITALIC
        if use_italic:
            font_parts.append("italic")
        font_str = " ".join(font_parts)
        return (
            f'  (gr_text "{text}" (at {x:.4f} {y:.4f}) (layer F.SilkS) (tstamp {_uuid()})'
            f'\n    (effects (font {font_str})))'
        )

    for (text, y_panel) in [(TITLE_TOP, TITLE_Y_TOP_MM), (TITLE_BOTTOM, TITLE_Y_BOTTOM_MM)]:
        x_center = TITLE_X_MM
        lines.append(gr_text_at(x_center, y_panel, text, size_mm))

    label_list = _match_labels_to_holes(holes)
    for (drill_x, drill_y, family, text, use_italic) in label_list:
        r_mm = _panel_cutout_diameter_mm(family) / 2.0
        w = _estimate_text_width_mm(text, size_mm)
        y_bottom = drill_y - r_mm - SILK_LABEL_OFFSET_BELOW_MM - size_mm
        y_center = y_bottom + size_mm / 2.0
        x_center = drill_x
        lines.append(gr_text_at(x_center, y_center, text, size_mm, italic=use_italic))
    return lines


def _parse_generated_pcb(pcb_path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float, float]]]:
    """Parse generated .kicad_pcb and return (circles (x,y,r), rects (x,y,w,h)).
    circles from footprint (at x y) + drill radius from footprint name.
    rects from gr_rect on Edge.Cuts.
    """
    text = pcb_path.read_text(encoding="utf-8", errors="ignore")
    # Footprint name (as in .kicad_pcb) -> panel cutout radius (mm). Must match _footprint_name().
    name_to_r: dict[str, float] = {
        "Panel_Mount_3p0mm": PANEL_MOUNT_MM / 2.0,
        "Panel_LED_3p2mm": PANEL_LED_MM / 2.0,
        "Panel_Jack_6p3mm": PANEL_JACK_MM / 2.0,
        "Panel_9MM_Pot_7p5mm": PANEL_POT_MM / 2.0,
        "Panel_Switch_B7_5p5mm": PANEL_SWITCH_B7_MM / 2.0,
        "Panel_Switch_B8_6p3mm": PANEL_SWITCH_B8_MM / 2.0,
    }

    circles: list[tuple[float, float, float]] = []
    rects: list[tuple[float, float, float, float]] = []
    in_fp = False
    fp_at_x, fp_at_y = 0.0, 0.0
    fp_name: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("(footprint "):
            in_fp = True
            parts = line.split('"', 2)
            if len(parts) >= 2:
                fp_name = parts[1].split(":")[-1]
            fp_at_x, fp_at_y = 0.0, 0.0
            continue
        if in_fp and line.startswith("(at "):
            tok = line.strip("()").split()
            if len(tok) >= 3:
                try:
                    fp_at_x, fp_at_y = float(tok[1]), float(tok[2])
                except ValueError:
                    pass
        if in_fp and line.startswith("(path "):
            in_fp = False
            if fp_name and fp_name in name_to_r:
                r = name_to_r[fp_name]
                circles.append((fp_at_x, fp_at_y, r))
            continue
        if "(gr_rect " in line and "Edge.Cuts" in line:
            m = re.search(
                r"\(start\s+([-\d.]+)\s+([-\d.]+)\)\s*\(end\s+([-\d.]+)\s+([-\d.]+)\)",
                line,
            )
            if m:
                x1, y1, x2, y2 = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
                rects.append((x1, y1, x2 - x1, y2 - y1))

    return circles, rects


def _parse_drill_slots_from_pcb(pcb_path: Path) -> list[tuple[float, float, float, float]]:
    """Parse Panel_Slot_* footprints from .kicad_pcb. Return list of (x, y, w, h) slot rect in board coords (x,y = top-left)."""
    text = pcb_path.read_text(encoding="utf-8", errors="ignore")
    slots: list[tuple[float, float, float, float]] = []
    in_fp = False
    fp_at_x, fp_at_y = 0.0, 0.0
    fp_name: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("(footprint "):
            in_fp = True
            parts = line.split('"', 2)
            if len(parts) >= 2:
                fp_name = parts[1].split(":")[-1]
            fp_at_x, fp_at_y = 0.0, 0.0
            continue
        if in_fp and line.startswith("(at "):
            tok = line.strip("()").split()
            if len(tok) >= 3:
                try:
                    fp_at_x, fp_at_y = float(tok[1]), float(tok[2])
                except ValueError:
                    pass
        if in_fp and line.startswith("(path "):
            in_fp = False
            if fp_name and fp_name.startswith("Panel_Slot_"):
                # Name like Panel_Slot_5p0x3p0 -> w=5.0, h=3.0
                rest = fp_name.replace("Panel_Slot_", "")
                parts = rest.split("x", 1)
                if len(parts) == 2:
                    try:
                        w = float(parts[0].replace("p", "."))
                        h = float(parts[1].replace("p", "."))
                        sx = fp_at_x - w / 2.0
                        sy = fp_at_y - h / 2.0
                        slots.append((sx, sy, w, h))
                    except ValueError:
                        pass
            continue
    return slots


def _rect_contained_in(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float], tol: float = 0.02) -> bool:
    """True if inner rect (x,y,w,h) is contained in outer rect (x,y,w,h) within tolerance."""
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return (
        ox <= ix + tol
        and oy <= iy + tol
        and ox + ow >= ix + iw - tol
        and oy + oh >= iy + ih - tol
    )


def _validate_drill_slots_contain_reference(
    pcb_path: Path,
    expected_slot_rects: list[tuple[float, float, float, float]],
    tol_mm: float = 0.02,
) -> list[str]:
    """When USE_DRILL_SLOTS: check each expected slot rect (board coords) is contained in some drill slot. Returns error list."""
    slots = _parse_drill_slots_from_pcb(pcb_path)
    errors: list[str] = []
    used = [False] * len(slots)
    for (ex, ey, ew, eh) in expected_slot_rects:
        contained = False
        for i, (sx, sy, sw, sh) in enumerate(slots):
            if _rect_contained_in((ex, ey, ew, eh), (sx, sy, sw, sh), tol_mm):
                contained = True
                used[i] = True
                break
        if not contained:
            errors.append(
                f"Reference slot rect (x={ex:.3f}, y={ey:.3f}, w={ew:.3f}, h={eh:.3f}) not contained in any drill slot"
            )
    return errors


def _validate_against_reference(
    pcb_path: Path,
    expected_circles: list[tuple[float, float, float]],
    expected_rects: list[tuple[float, float, float, float]],
    tol_mm: float = 0.02,
) -> list[str]:
    """Compare generated PCB geometry to expected. Returns list of error messages."""
    actual_circles, actual_rects = _parse_generated_pcb(pcb_path)
    errors: list[str] = []

    if len(actual_circles) != len(expected_circles):
        errors.append(
            f"Circle count mismatch: expected {len(expected_circles)}, got {len(actual_circles)}"
        )
    else:
        used = [False] * len(actual_circles)
        for (ex, ey, er) in expected_circles:
            best_i = -1
            best_dist = 1e9
            for i, (ax, ay, ar) in enumerate(actual_circles):
                if used[i]:
                    continue
                d = ((ax - ex) ** 2 + (ay - ey) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best_i = i
            if best_i < 0:
                errors.append(f"No matching drill for expected circle at ({ex:.3f}, {ey:.3f}) r={er:.3f}")
            else:
                used[best_i] = True
                ax, ay, ar = actual_circles[best_i]
                if abs(ax - ex) > tol_mm or abs(ay - ey) > tol_mm:
                    errors.append(
                        f"Drill position mismatch: expected ({ex:.3f}, {ey:.3f}), got ({ax:.3f}, {ay:.3f})"
                    )
                if abs(ar - er) > tol_mm:
                    errors.append(
                        f"Drill radius mismatch at ({ax:.3f}, {ay:.3f}): expected r={er:.3f}, got r={ar:.3f}"
                    )

    if len(actual_rects) != len(expected_rects):
        errors.append(
            f"Rect count mismatch: expected {len(expected_rects)}, got {len(actual_rects)}"
        )
    else:
        for (ex, ey, ew, eh) in expected_rects:
            matched = False
            for (ax, ay, aw, ah) in actual_rects:
                if (
                    abs(ax - ex) <= tol_mm
                    and abs(ay - ey) <= tol_mm
                    and abs(aw - ew) <= tol_mm
                    and abs(ah - eh) <= tol_mm
                ):
                    matched = True
                    break
            if not matched:
                errors.append(
                    f"Edge cutout rect not found: expected (x={ex:.3f}, y={ey:.3f}, w={ew:.3f}, h={eh:.3f})"
                )

    return errors


def _extract_sexpr_section(text: str, section_name: str) -> str | None:
    """Extract a top-level (section_name ...) from KiCad PCB text. Looks for '  (section_name' to preserve indent. Returns None if not found."""
    needle = "  (" + section_name
    start = text.find(needle)
    if start == -1:
        return None
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def _board_settings_from_reference(ref_path: Path, aux_axis_origin_x_mm: float, aux_axis_origin_y_mm: float) -> tuple[str, str, str, str]:
    """Read board settings (general, paper, setup) from reference PCB; layers kept as default so Dwgs.User etc. remain. Injects aux_axis_origin into setup."""
    default_general = """  (general
    (thickness 1.6)
  )"""
    default_paper = '  (paper A4)'
    default_layers = """  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user "B.Fabrication")
    (49 "F.Fab" user "F.Fabrication")
  )"""
    default_setup = f"""  (setup
    (stackup
      (layer "copper" "F.Cu" (type signal) (thickness 0.035))
      (layer "dielectric" "core" (type core) (thickness 1.53) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "copper" "B.Cu" (type signal) (thickness 0.035))
    )
    (pad_to_mask_clearance 0.051)
    (solder_mask_min_width 0.25)
    (aux_axis_origin {aux_axis_origin_x_mm} {aux_axis_origin_y_mm})
  )"""

    if not ref_path.exists():
        return default_general, default_paper, default_layers, default_setup

    text = ref_path.read_text(encoding="utf-8", errors="replace")
    general = _extract_sexpr_section(text, "general") or default_general
    paper = _extract_sexpr_section(text, "paper") or default_paper
    # Always use default_layers so Dwgs.User etc. exist for panel footprints
    layers = default_layers
    setup_raw = _extract_sexpr_section(text, "setup") or default_setup

    # Inject aux_axis_origin into setup if missing (reference may not have it)
    aux_line = f"    (aux_axis_origin {aux_axis_origin_x_mm} {aux_axis_origin_y_mm})"
    if "aux_axis_origin" not in setup_raw:
        setup_raw = setup_raw.rstrip()
        if setup_raw.endswith("\n  )"):
            setup_raw = setup_raw[:-4] + "\n" + aux_line + "\n  )"
        else:
            setup_raw = setup_raw[:-1] + "\n" + aux_line + "\n  )"
    else:
        setup_raw = re.sub(
            r"\s*\(aux_axis_origin\s+[-\d.]+\s+[-\d.]+\)",
            "\n    " + aux_line,
            setup_raw,
            count=1,
        )

    return general, paper, layers, setup_raw


def build_kicad_pcb(
    holes: list[tuple[float, float, str]],
    sd_slot: tuple[float, float, float, float] | None,
    out_path: Path,
    pretty_path: Path,
    lib_name: str,
) -> None:
    """Write generated_panel.kicad_pcb and panel footprint library."""
    pretty_path.mkdir(parents=True, exist_ok=True)

    # Current set of footprint names we write
    current_names = {_footprint_name(f) for f in (FAMILY_MOUNT, FAMILY_LED, FAMILY_POT, FAMILY_JACK, FAMILY_SWITCH_B7, FAMILY_SWITCH_B8)}
    if USE_DRILL_SLOTS:
        screw_rects_list: list[tuple[float, float, float, float]] = list(_screw_slot_rects())
        slot_rects = screw_rects_list + ([sd_slot] if sd_slot is not None else [])
        for i, (_, _, sw, sh) in enumerate(slot_rects):
            slot_w = sw + SLOT_OVERSIZE_MM
            slot_h = sh + SLOT_OVERSIZE_MM
            if i < len(screw_rects_list):
                slot_h = min(slot_h, MOUNT_SLOT_MAX_HEIGHT_MM)
            current_names.add(_slot_footprint_name(slot_w, slot_h))
    for f in pretty_path.glob("*.kicad_mod"):
        if f.stem not in current_names:
            f.unlink()

    for family in (FAMILY_MOUNT, FAMILY_LED, FAMILY_POT, FAMILY_JACK, FAMILY_SWITCH_B7, FAMILY_SWITCH_B8):
        _write_panel_footprint(pretty_path, family, lib_name)
    if USE_DRILL_SLOTS:
        screw_rects_list = list(_screw_slot_rects())
        slot_rects = screw_rects_list + ([sd_slot] if sd_slot is not None else [])
        seen_sizes: set[tuple[float, float]] = set()
        for i, (_, _, sw, sh) in enumerate(slot_rects):
            slot_w = sw + SLOT_OVERSIZE_MM
            slot_h = sh + SLOT_OVERSIZE_MM
            if i < len(screw_rects_list):
                slot_h = min(slot_h, MOUNT_SLOT_MAX_HEIGHT_MM)
            key = (round(slot_w, 4), round(slot_h, 4))
            if key not in seen_sizes:
                seen_sizes.add(key)
                _write_slot_footprint(pretty_path, slot_w, slot_h, lib_name)

    screw_rects = _screw_slot_rects()
    w, h = PANEL_WIDTH_MM, PANEL_HEIGHT_MM
    ox, oy = PANEL_OFFSET_X_MM, PANEL_OFFSET_Y_MM

    edge_lines = [
        f'  (gr_line (start {ox:.4f} {oy:.4f}) (end {ox + w:.4f} {oy:.4f}) (layer Edge.Cuts) (width 0.05) (tstamp {_uuid()}))',
        f'  (gr_line (start {ox + w:.4f} {oy:.4f}) (end {ox + w:.4f} {oy + h:.4f}) (layer Edge.Cuts) (width 0.05) (tstamp {_uuid()}))',
        f'  (gr_line (start {ox + w:.4f} {oy + h:.4f}) (end {ox:.4f} {oy + h:.4f}) (layer Edge.Cuts) (width 0.05) (tstamp {_uuid()}))',
        f'  (gr_line (start {ox:.4f} {oy + h:.4f}) (end {ox:.4f} {oy:.4f}) (layer Edge.Cuts) (width 0.05) (tstamp {_uuid()}))',
    ]
    if not USE_DRILL_SLOTS:
        if sd_slot:
            sx, sy, sw, sh = sd_slot
            edge_lines.append(
                f'  (gr_rect (start {sx + ox:.4f} {sy + oy:.4f}) (end {sx + sw + ox:.4f} {sy + sh + oy:.4f}) '
                f'(layer Edge.Cuts) (width 0.05) (tstamp {_uuid()}))'
            )
        for (sx, sy, sw, sh) in screw_rects:
            edge_lines.append(
                f'  (gr_rect (start {sx + ox:.4f} {sy + oy:.4f}) (end {sx + sw + ox:.4f} {sy + sh + oy:.4f}) '
                f'(layer Edge.Cuts) (width 0.05) (tstamp {_uuid()}))'
            )

    pattern_segs = _pattern_segments_three_zone(
        ox - PATTERN_EXTEND_OUTSIDE_MM,
        oy - PATTERN_EXTEND_OUTSIDE_MM,
        w + 2.0 * PATTERN_EXTEND_OUTSIDE_MM,
        h + 2.0 * PATTERN_EXTEND_OUTSIDE_MM,
        PATTERN_SCALE_A,
        PATTERN_SCALE_B,
    )
    pattern_trace_mm = PATTERN_TRACE_WIDTH_MM * (PATTERN_SCALE_A + PATTERN_SCALE_B) / 2.0
    pattern_lines: list[str] = []
    for item in pattern_segs:
        if item[0] == "segment":
            _, x1, y1, x2, y2 = item
            pattern_lines.append(
                f'  (segment (start {x1:.4f} {y1:.4f}) (end {x2:.4f} {y2:.4f}) (width {pattern_trace_mm:.4f}) (layer F.Cu) (net 0) (tstamp {_uuid()}))'
            )
        else:
            _, xs, ys, xm, ym, xe, ye = item
            pattern_lines.append(
                f'  (arc (start {xs:.4f} {ys:.4f}) (mid {xm:.4f} {ym:.4f}) (end {xe:.4f} {ye:.4f}) (width {pattern_trace_mm:.4f}) (layer F.Cu) (net 0) (tstamp {_uuid()}))'
            )

    footprint_instances = []
    for (hx, hy, family) in holes:
        if family == FAMILY_MOUNT:
            continue
        mod_name = _footprint_name(family)
        body_lines = _footprint_body_lines(family)
        path_uuid = _uuid()
        footprint_instances.append(
            f'  (footprint "{lib_name}:{mod_name}" (layer F.Cu) (tstamp {_uuid()})\n'
            f'    (at {hx + ox:.4f} {hy + oy:.4f})\n'
            f'    (path /{path_uuid})\n'
            + "\n".join(body_lines)
            + ")"
        )
    if USE_DRILL_SLOTS:
        screw_rects_list = list(_screw_slot_rects())
        slot_rects = screw_rects_list + ([sd_slot] if sd_slot is not None else [])
        for i, (sx, sy, sw, sh) in enumerate(slot_rects):
            slot_w = sw + SLOT_OVERSIZE_MM
            slot_h = sh + SLOT_OVERSIZE_MM
            if i < len(screw_rects_list):
                slot_h = min(slot_h, MOUNT_SLOT_MAX_HEIGHT_MM)
            cx = ox + sx + sw / 2.0
            cy = oy + sy + sh / 2.0
            mod_name = _slot_footprint_name(slot_w, slot_h)
            body_lines = _slot_footprint_body_lines(slot_w, slot_h)
            path_uuid = _uuid()
            footprint_instances.append(
                f'  (footprint "{lib_name}:{mod_name}" (layer F.Cu) (tstamp {_uuid()})\n'
                f'    (at {cx:.4f} {cy:.4f})\n'
                f'    (path /{path_uuid})\n'
                + "\n".join(body_lines)
                + ")"
            )

    general, paper, layers, setup = _board_settings_from_reference(
        BOARD_SETTINGS_REFERENCE_PCB, GRID_ORIGIN_X_MM, GRID_ORIGIN_Y_MM
    )
    pcb_content = f"""(kicad_pcb (version 20221018) (generator "resynthesis-panel")

{general}

{paper}
{layers}

{setup}

  (net 0 "")

  (net_class Default "Default"
    (clearance 0.2)
    (trace_width 0.25)
    (via_dia 0.8)
    (via_drill 0.4)
    (uvia_dia 0.3)
    (uvia_drill 0.1)
    (add_net 0)
  )

"""
    pcb_content += "\n".join(footprint_instances) + "\n\n"
    pcb_content += "\n".join(pattern_lines) + "\n\n"
    silkscreen_rect_lines = _silkscreen_output_jack_rect_lines(ox, oy)
    silkscreen_text_lines = _silkscreen_gr_text_lines(ox, oy, holes)
    pcb_content += "\n".join(silkscreen_rect_lines) + "\n\n"
    pcb_content += "\n".join(silkscreen_text_lines) + "\n\n"
    # Solder mask: only behind labels and silkscreen shapes (e.g. output jack rounded rects); rest exposed copper.
    label_mask_rects = _label_mask_rects_board_mm(holes, ox, oy)
    silkscreen_polygons = _silkscreen_shape_polygons_board_mm(ox, oy)
    mask_zone_lines = _solder_mask_zone_lines(ox, oy, w, h, label_mask_rects, silkscreen_polygons)
    pcb_content += "\n".join(mask_zone_lines) + "\n\n"
    pcb_content += "\n".join(edge_lines) + "\n"
    pcb_content += ")\n"

    out_path.write_text(pcb_content, encoding="utf-8")


def build_kicad_pro(project_dir: Path, pcb_name: str = "generated_panel") -> None:
    """Write minimal .kicad_pro."""
    pro_path = project_dir / f"{pcb_name}.kicad_pro"
    pro_content = '''{
  "board": {
    "design_settings": {
      "defaults": {
        "board_outline_line_width": 0.1,
        "copper_line_width": 0.2,
        "other_line_width": 0.15,
        "silk_line_width": 0.15
      }
    }
  },
  "schematic": {},
  "meta": {
    "filename": "generated_panel.kicad_pro",
    "version": 1
  }
}
'''
    pro_path.write_text(pro_content, encoding="utf-8")


def _write_fp_lib_table(project_dir: Path, lib_name: str) -> None:
    """Write fp-lib-table so KiCad finds the panel footprint library."""
    table_path = project_dir / "fp-lib-table"
    table_path.write_text(
        f'(fp_lib_table\n'
        f'  (lib (name {lib_name})(type KiCad)(uri "${{KIPRJMOD}}/{lib_name}.pretty")(options "")(descr "Panel cutouts + component outline (Dwgs.User)"))\n'
        f')\n',
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate KiCad PCB for Resynthesis panel (patch_init dimensions, custom footprints, validation)."
    )
    parser.add_argument(
        "kicad_pcb",
        nargs="?",
        type=Path,
        default=DEFAULT_KICAD_PCB,
        help="Patch.Init KiCad PCB file",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory (default: panel/output/generated_panel)",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip validation against hardware-centers reference",
    )
    args = parser.parse_args()

    kicad_path = args.kicad_pcb.resolve()
    if not kicad_path.exists():
        raise SystemExit(f"KiCad PCB not found: {kicad_path}")

    holes = _parse_holes_from_kicad(kicad_path)
    if not holes:
        raise SystemExit("No front-panel holes parsed from KiCad PCB.")

    sd_slot = _parse_sd_slot_from_kicad(kicad_path)
    all_holes = holes + _mount_holes()

    out_dir = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pcb_name = "generated_panel"
    lib_name = "generated_panel"
    pcb_path = out_dir / f"{pcb_name}.kicad_pcb"
    pretty_path = out_dir / f"{lib_name}.pretty"

    build_kicad_pcb(all_holes, sd_slot, pcb_path, pretty_path, lib_name)
    build_kicad_pro(out_dir, pcb_name)
    _write_fp_lib_table(out_dir, lib_name)

    # Write reference SVG (same content as hardware-centers-kicad.svg)
    ref_svg = out_dir / "hardware-centers-kicad.svg"
    _write_hardware_centers_svg(ref_svg, all_holes, sd_slot)

    if not args.no_validate:
        expected_circles, expected_rects = _build_reference_geometry(all_holes, sd_slot)
        ox, oy = PANEL_OFFSET_X_MM, PANEL_OFFSET_Y_MM
        expected_circles_placed = [(x + ox, y + oy, r) for (x, y, r) in expected_circles]
        if USE_DRILL_SLOTS:
            expected_rects_placed = []
            expected_slot_rects_placed = [(x + ox, y + oy, w, h) for (x, y, w, h) in expected_rects]
        else:
            expected_rects_placed = [(x + ox, y + oy, w, h) for (x, y, w, h) in expected_rects]
            expected_slot_rects_placed = []
        errors = _validate_against_reference(pcb_path, expected_circles_placed, expected_rects_placed)
        if USE_DRILL_SLOTS:
            errors.extend(_validate_drill_slots_contain_reference(pcb_path, expected_slot_rects_placed))
        if errors:
            raise SystemExit(
                "Validation failed (cutout/drill vs hardware-centers-kicad reference):\n  "
                + "\n  ".join(errors)
            )
        print("Validation passed: drill/cutout layer matches hardware-centers-kicad reference.")

    print(f"Wrote KiCad project → {out_dir}")
    print(f"  PCB: {pcb_path.name} ({PANEL_WIDTH_MM}×{PANEL_HEIGHT_MM} mm)")
    print(f"  Footprints: {pretty_path.name}/")
    print(f"  Holes: {len(all_holes)} (components + mount), SD slot: {'yes' if sd_slot else 'no'}")
    print(f"  Reference: {ref_svg.name}")


if __name__ == "__main__":
    main()
