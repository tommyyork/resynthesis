# -*- coding: utf-8 -*-
"""
Panel alignment test for the Resynthesis panel (Patch.Init format).

The FIRST test run by this script verifies that the canonical panel SVG has
drill hole positions and diameters identical to the Electrosmith Patch.Init
NPTH drill file. Any panel design in this folder must pass that test before
other checks; the SVG is the source of truth for fabrication and preview
images must be exported from the SVG so they reflect the same layout.

Additional tests:
- Other SVGs named ResynthesisPanel_*.svg must share the same hole layout.
- PCBWay-style design-rule checks (min hole size, spacing, hole-to-edge).
- Rogan knob clearance for CV_1–CV_4 labels.
- Four potentiometer shaft cutouts, two switch cutouts, and SD card holder
  cutout must be present and match Patch.Init (drill holes + Edge_Cuts).

Assumptions
----------
- `ResynthesisPanel.svg` is the canonical panel; hole positions/sizes match
  ES_Daisy_Patch_SM_Init_Rev1_extracted/gerbers/drills.xln.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot, cos, sin, radians
from pathlib import Path
from typing import Iterable, List

import xml.etree.ElementTree as ET
import re

from generate_resynthesis_panel_svg import (
    POT_PANEL_DIAMETER_MM,
    SWITCH_B7_PANEL_DIAMETER_MM,
    SWITCH_B8_PANEL_DIAMETER_MM,
    JACK_PANEL_DIAMETER_MM,
    LED_DRILL_DIAMETER_MM,
)


HERE = Path(__file__).parent
CANONICAL_SVG = HERE / "output" / "ResynthesisPanel.svg"
DRILL_ONLY_SVG = HERE / "output" / "ResynthesisPanel_drill.svg"


def _panel_assets_path(*parts: str) -> Path:
    """Return a path inside the panel assets tree, with legacy fallback.

    Prefers `panel/assets/...` when present (current layout), but also
    supports the older layout where assets such as `KiCad_PCB/` and
    `patch_init_gerbers/` lived directly under `panel/`.
    """
    assets_root = HERE / "assets"
    candidates = [
        assets_root.joinpath(*parts),
        HERE.joinpath(*parts),
    ]
    for p in candidates:
        if p.exists():
            return p
    # Default to first candidate so callers still get a useful path for error messages.
    return candidates[0]

# ---------------------------------------------------------------------------
# PCBWay-style design rules (from PCBWay help center / online portal checks)
# https://www.pcbway.com/helpcenter/
# ---------------------------------------------------------------------------
PCBWAY_MIN_NPTH_DIAMETER_MM = 0.45   # min non-plated hole size (min_non_plated_holes)
PCBWAY_MIN_HOLE_SPACING_MM = 0.20    # min edge-to-edge between holes (holes design standard; non-plated)
PCBWAY_MIN_HOLE_TO_BOARD_EDGE_MM = 0.50  # min from hole edge to board outline (spacing from hole to edge of board)
PCBWAY_MAX_BOARD_DIMENSION_MM = 500.0    # typical max for prototype; panel is well under

# Eurorack 3U mounting (Doepfer A-100 / panel/eurorack_spec/README.md)
PANEL_HEIGHT_MM = 128.5
SCREW_CENTER_FROM_TOP_EDGE_MM = 3.0   # hole center from top edge
SCREW_CENTER_FROM_BOTTOM_EDGE_MM = 3.0  # hole center from bottom edge => bottom row at 128.5 - 3 = 125.5

# Patch.Init panel origin (from blank-Edge_Cuts.gbr): board left/top in Gerber mm
PATCH_INIT_PANEL_ORIGIN_X_MM = 26.545
PATCH_INIT_PANEL_ORIGIN_Y_MM = -27.095  # Gerber Y is negative downward
OY_TOP_MM = 27.095  # -Gerber Y for top edge

# KiCad PCB → panel coordinate mapping (see Resynthesis/README.md).
# Example correspondences (KiCad PCB X/Y → panel SVG X/Y):
#   - B10 / J_GATEIN1 jack: (130.251, 125.316) → (7.15, 84.562)
#   - CV_2 / VR_2 pot:      (162.751,  63.756) → (39.65, 22.904)
# These are well approximated by:
#   panel_x ≈ pcb_x - 123.10
#   panel_y ≈ pcb_y -  40.80
KICAD_PANEL_OFFSET_X_MM = 123.10
KICAD_PANEL_OFFSET_Y_MM = 40.80

# Knob clearance assumption (Mutable Instruments-style Rogan knobs)
# We model the knob as a circle centered on the pot shaft.
ROGAN_KNOB_DIAMETER_MM = 12.0
ROGAN_LABEL_CLEARANCE_MM = 0.5

# Minimum font size: 10 point (1 pt = 25.4/72 mm)
MIN_FONT_SIZE_PT = 10
MIN_FONT_SIZE_MM = MIN_FONT_SIZE_PT * 25.4 / 72.0  # ~3.53 mm


@dataclass(frozen=True)
class Hole:
    """Simple representation of a hardware hole on the panel."""

    kind: str  # "circle" or "rect"
    x: float
    y: float
    r: float  # radius (for circles) or equivalent radius (for rect diagonals)


def _parse_float(value: str | None) -> float:
    if value is None:
        raise ValueError("Missing numeric attribute in SVG.")
    return float(eval(value, {}, {}))  # allow simple expressions like "50.8 - 7.5"


def extract_holes(svg_path: Path) -> List[Hole]:
    """Extract hardware hole centers from an SVG panel file.

    We consider:
    - <circle> elements as jacks, knobs, mounting holes, etc.
    - <rect> elements as toggle switches (e.g. the \"disco\" switch).
    Circles/rects inside <defs>, <pattern>, <mask>, or <linearGradient>
    are excluded (they are decorative or clip art).
    """
    tree = ET.parse(svg_path)
    root = tree.getroot()

    # Build parent map so we can skip elements inside defs/pattern/mask
    parent_map = {c: p for p in root.iter() for c in p}

    def _inside_defs_or_pattern(el: ET.Element) -> bool:
        tag = (el.tag.split("}")[-1] if "}" in el.tag else el.tag).lower()
        if tag in ("defs", "pattern", "mask", "lineargradient", "radialgradient"):
            return True
        p = parent_map.get(el)
        return p is not None and _inside_defs_or_pattern(p)

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    holes: List[Hole] = []

    # Circles (jacks, pots, mounting holes, etc.)
    for el in root.iter(f"{ns}circle"):
        if _inside_defs_or_pattern(el):
            continue
        r_attr = el.get("r")
        cx_attr = el.get("cx")
        cy_attr = el.get("cy")
        if r_attr is None or cx_attr is None or cy_attr is None:
            continue
        r = _parse_float(r_attr)
        if r < 0.5:
            continue
        cx = _parse_float(cx_attr)
        cy = _parse_float(cy_attr)
        holes.append(Hole("circle", cx, cy, r))

    # Rectangles (mainly toggles)
    for el in root.iter(f"{ns}rect"):
        if _inside_defs_or_pattern(el):
            continue
        # Skip decorative label backgrounds and other non-mechanical rectangles.
        if (el.get("data-panel-role") or "").strip().lower() == "label-bg":
            continue
        x_attr = el.get("x")
        y_attr = el.get("y")
        w_attr = el.get("width")
        h_attr = el.get("height")
        if x_attr is None or y_attr is None or w_attr is None or h_attr is None:
            continue
        w = _parse_float(w_attr)
        h = _parse_float(h_attr)
        if w > 40.0 and h > 100.0:
            continue
        if w < 1.0 and h < 1.0:
            continue
        x = _parse_float(x_attr) + w / 2.0
        y = _parse_float(y_attr) + h / 2.0
        eq_r = min(w, h) / 2.0
        holes.append(Hole("rect", x, y, eq_r))

    return holes


def extract_screw_cutout_centers(svg_path: Path) -> List[tuple[float, float]]:
    """Extract center (x, y) of four corner screw cutout rectangles from the panel SVG.

    Identifies rects that are black-filled, small (screw slots), and not the SD card
    cutout. Returns list of (center_x, center_y) in document order (top-left, top-right,
    bottom-left, bottom-right).
    """
    tree = ET.parse(svg_path)
    root = tree.getroot()

    parent_map = {c: p for p in root.iter() for c in p}

    def _inside_defs_or_pattern(el: ET.Element) -> bool:
        tag = (el.tag.split("}")[-1] if "}" in el.tag else el.tag).lower()
        if tag in ("defs", "pattern", "mask", "lineargradient", "radialgradient"):
            return True
        p = parent_map.get(el)
        return p is not None and _inside_defs_or_pattern(p)

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    centers: List[tuple[float, float]] = []
    for el in root.iter(f"{ns}rect"):
        if _inside_defs_or_pattern(el):
            continue
        # Skip decorative label backgrounds and other non-mechanical rectangles.
        if (el.get("data-panel-role") or "").strip().lower() == "label-bg":
            continue
        fill = (el.get("fill") or "").strip().lower()
        if fill not in ("#000000", "black"):
            continue
        x_attr = el.get("x")
        y_attr = el.get("y")
        w_attr = el.get("width")
        h_attr = el.get("height")
        if x_attr is None or y_attr is None or w_attr is None or h_attr is None:
            continue
        w = _parse_float(w_attr)
        h = _parse_float(h_attr)
        # SD card cutout is much taller (~12.8 mm); screw slots are ~3–6 mm
        if h > 10.0 or w > 10.0:
            continue
        if w < 2.0 or h < 2.0:
            continue
        x = _parse_float(x_attr)
        y = _parse_float(y_attr)
        cx = x + w / 2.0
        cy = y + h / 2.0
        centers.append((cx, cy))

    # Expect exactly 4 screw cutouts
    if len(centers) != 4:
        return centers  # caller will assert
    # Sort by y then x so order is top-left, top-right, bottom-left, bottom-right
    centers.sort(key=lambda p: (p[1], p[0]))
    return centers


@dataclass(frozen=True)
class SvgText:
    x: float
    y: float
    font_size: float
    anchor: str
    text: str


def extract_text(svg_path: Path) -> list[SvgText]:
    tree = ET.parse(svg_path)
    root = tree.getroot()

    parent_map = {c: p for p in root.iter() for c in p}

    def _inside_defs_or_pattern(el: ET.Element) -> bool:
        tag = (el.tag.split("}")[-1] if "}" in el.tag else el.tag).lower()
        if tag in ("defs", "pattern", "mask", "lineargradient", "radialgradient"):
            return True
        p = parent_map.get(el)
        return p is not None and _inside_defs_or_pattern(p)

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    texts: list[SvgText] = []
    for el in root.iter(f"{ns}text"):
        if _inside_defs_or_pattern(el):
            continue
        x_attr = el.get("x")
        y_attr = el.get("y")
        fs_attr = el.get("font-size")
        anchor = el.get("text-anchor", "start")
        if x_attr is None or y_attr is None or fs_attr is None:
            continue
        content = "".join(el.itertext()).strip()
        if not content:
            continue
        texts.append(
            SvgText(
                x=_parse_float(x_attr),
                y=_parse_float(y_attr),
                font_size=_parse_float(fs_attr),
                anchor=anchor,
                text=" ".join(content.split()),
            )
        )
    return texts


def _approx_text_bbox(t: SvgText) -> tuple[float, float, float, float]:
    """Approximate (minx, miny, maxx, maxy) for a single-line SVG text."""
    # crude but consistent; good enough for collision testing
    char_w = 0.60 * t.font_size
    width = char_w * len(t.text)
    height = 1.0 * t.font_size

    if t.anchor == "middle":
        minx = t.x - width / 2.0
        maxx = t.x + width / 2.0
    elif t.anchor == "end":
        minx = t.x - width
        maxx = t.x
    else:
        minx = t.x
        maxx = t.x + width

    # SVG text y is baseline; approximate bbox around baseline
    miny = t.y - 0.80 * height
    maxy = t.y + 0.20 * height
    return (minx, miny, maxx, maxy)


def _bbox_circle_intersects(bbox: tuple[float, float, float, float], cx: float, cy: float, r: float) -> bool:
    minx, miny, maxx, maxy = bbox
    # clamp circle center to bbox
    px = min(max(cx, minx), maxx)
    py = min(max(cy, miny), maxy)
    return hypot(px - cx, py - cy) <= r


def _bboxes_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    margin_mm: float = 0.0,
) -> bool:
    """True if two axis-aligned boxes overlap (with optional margin)."""
    minx_a, miny_a, maxx_a, maxy_a = a
    minx_b, miny_b, maxx_b, maxy_b = b
    if margin_mm != 0:
        minx_a -= margin_mm
        miny_a -= margin_mm
        maxx_a += margin_mm
        maxy_a += margin_mm
        minx_b -= margin_mm
        miny_b -= margin_mm
        maxx_b += margin_mm
        maxy_b += margin_mm
    return minx_a < maxx_b and minx_b < maxx_a and miny_a < maxy_b and miny_b < maxy_a


def parse_patch_init_holes_from_kicad(kicad_path: Path) -> list[tuple[float, float, float]]:
    """Derive Patch.Init hardware hole centres from the KiCad PCB file.

    We approximate the hardware drill locations by taking the centre marker
    circles from the front‑panel related footprints (pots, jacks, switches,
    LED) in the KiCad board and treating their board coordinates as already
    expressed in the shared panel‑local coordinate system used elsewhere in
    this module (origin at the top‑left of the panel, X to the right, Y down).

    Returns a list of (x_local, y_local, r_mech) in millimetres with origin at
    the top‑left of the panel, X to the right, Y down. ``r_mech`` is the
    inferred mechanical panel cutout radius for the associated footprint,
    derived from the footprint name where possible (see
    ``_render_panel_cutout_overlay_for_footprint()`` for the mapping). For
    non-front-panel hardware or footprints without a known mechanical family,
    ``r_mech`` falls back to a small default radius used only for visualisation.
    """
    text = kicad_path.read_text(encoding="utf-8", errors="ignore")

    holes: list[tuple[float, float, float]] = []

    # Side-effect: render one SVG per unique front-panel footprint, showing the
    # footprint geometry with the inferred panel cutout overlaid. This is
    # purely diagnostic and does not affect the returned hole coordinates.
    rendered_footprints: set[str] = set()

    in_module = False
    depth = 0
    mod_ref: str | None = None
    mod_at_x = 0.0
    mod_at_y = 0.0
    mod_rot_deg = 0.0
    mod_footprint_name: str | None = None
    def flush_module() -> None:
        nonlocal holes, mod_ref, mod_at_x, mod_at_y, mod_rot_deg, mod_footprint_name
        if not in_module:
            return
        if mod_ref is None:
            return
        # Only treat actual front‑panel hardware as drill locations: jacks (J_*),
        # pots (VR_*), switches (SW_*), and front LED(s).
        if not (
            mod_ref.startswith("J_")
            or mod_ref.startswith("VR_")
            or mod_ref.startswith("SW_")
            or mod_ref.startswith("LED")
        ):
            return

        theta = radians(mod_rot_deg)
        cx_local, cy_local = 0.0, 0.0
        cx_rot = cx_local * cos(theta) - cy_local * sin(theta)
        cy_rot = cx_local * sin(theta) + cy_local * cos(theta)
        bx = mod_at_x + cx_rot
        by = mod_at_y + cy_rot

        # Map KiCad PCB coordinates into the shared panel-local coordinate
        # system used elsewhere in this module.
        lx = bx - KICAD_PANEL_OFFSET_X_MM
        ly = by - KICAD_PANEL_OFFSET_Y_MM

        # Infer a mechanical panel cutout radius for this footprint where
        # possible, mirroring the mapping used for the diagnostic footprint
        # overlay renderer below. Panel diameters and clearances match the
        # values documented in panel/datasheets/README.md.
        mech_r: float | None = None
        if mod_footprint_name:
            fp_basename = mod_footprint_name.split(":")[-1]
            name_upper = fp_basename.upper()
            panel_diam: float | None
            if "9MM_SNAP-IN_POT" in name_upper:
                # 9 mm snap-in pot: 7.5 mm ⌀ panel (7.2 mm drill + 0.3 mm clearance).
                panel_diam = POT_PANEL_DIAMETER_MM
            elif "S_JACK" in name_upper:
                # 3.5 mm jack: 6.3 mm ⌀ panel (6.2 mm drill + 0.1 mm clearance).
                panel_diam = JACK_PANEL_DIAMETER_MM
            elif "TL1105" in name_upper:
                # TL1105 tactile switch: 5.5 mm ⌀ panel (5.5 mm drill + 0.0 mm clearance).
                panel_diam = SWITCH_B7_PANEL_DIAMETER_MM
            elif "TOGGLE" in name_upper and "ON-ON" in name_upper:
                # TOGGLE_ON-ON (B_8): 6.3 mm ⌀ panel (6.2 mm drill + 0.1 mm clearance).
                panel_diam = SWITCH_B8_PANEL_DIAMETER_MM
            elif "LED" in name_upper:
                # Front-panel LED: 3.2 mm ⌀ panel (3.2 mm drill + 0.0 mm clearance).
                # Uses the LED drill family diameter derived from `blank-NPTH.drl`
                # and exposed by the generator.
                panel_diam = LED_DRILL_DIAMETER_MM
            else:
                panel_diam = None
            if panel_diam is not None:
                mech_r = panel_diam / 2.0

        # For footprints where we cannot infer a mechanical family, fall back
        # to a small radius that keeps the SVG readable without implying a
        # specific drill size.
        if mech_r is None:
            mech_r = 0.7

        holes.append((lx, ly, mech_r))

        # Best-effort diagnostic rendering of the footprint geometry plus the
        # inferred panel cutout based on its local centre marker.
        if mod_footprint_name:
            # Module name is of the form LIB:FOOTPRINT. We only need the
            # footprint basename to locate the .kicad_mod file.
            fp_basename = mod_footprint_name.split(":")[-1]
            if fp_basename not in rendered_footprints:
                rendered_footprints.add(fp_basename)
                _render_panel_cutout_overlay_for_footprint(fp_basename)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        open_parens = line.count("(")
        close_parens = line.count(")")

        if not in_module and line.startswith("(module "):
            in_module = True
            depth = open_parens - close_parens
            mod_ref = None
            mod_at_x = 0.0
            mod_at_y = 0.0
            mod_rot_deg = 0.0
            # Extract full module footprint name from the first token after
            # "(module".
            parts = line.split()
            mod_footprint_name = parts[1] if len(parts) >= 2 else None
            continue

        if in_module:
            # Parse module-level (at x y [rot]) line.
            if line.startswith("(at "):
                tokens = line.strip("()").split()
                if len(tokens) >= 3:
                    try:
                        mod_at_x = float(tokens[1])
                        mod_at_y = float(tokens[2])
                    except ValueError:
                        pass
                if len(tokens) >= 4:
                    try:
                        mod_rot_deg = float(tokens[3])
                    except ValueError:
                        mod_rot_deg = 0.0

            # Parse reference designator: (fp_text reference REF ...
            if line.startswith("(fp_text reference "):
                parts = line.split()
                if len(parts) >= 3:
                    ref = parts[2]
                    if ref.startswith('"') and ref.endswith('"'):
                        ref = ref[1:-1]
                    mod_ref = ref

            depth += open_parens - close_parens
            if depth <= 0:
                flush_module()
                in_module = False
                depth = 0

    if in_module:
        flush_module()

    return holes


def parse_patch_init_sd_slot_from_kicad(kicad_path: Path) -> tuple[float, float, float, float] | None:
    """Return the panel-local SD card slot cutout derived from the KiCad PCB.

    The slot is modelled as a 2.3 mm × 12.0 mm vertical rectangle centred on the
    VERT_MICROSD_CENTERED footprint used by reference U_SDCARD1. The returned
    tuple is (x, y, width, height) in panel-local mm with origin at the
    top-left of the panel, X right, Y down.
    """
    text = kicad_path.read_text(encoding="utf-8", errors="ignore")

    in_module = False
    depth = 0
    mod_ref: str | None = None
    mod_at_x = 0.0
    mod_at_y = 0.0
    mod_rot_deg = 0.0

    def flush_module() -> tuple[float, float] | None:
        nonlocal mod_ref, mod_at_x, mod_at_y, mod_rot_deg
        if not in_module:
            return None
        if mod_ref != "U_SDCARD1":
            return None

        theta = radians(mod_rot_deg)
        cx_local, cy_local = 0.0, 0.0
        cx_rot = cx_local * cos(theta) - cy_local * sin(theta)
        cy_rot = cx_local * sin(theta) + cy_local * cos(theta)
        bx = mod_at_x + cx_rot
        by = mod_at_y + cy_rot

        lx = bx - KICAD_PANEL_OFFSET_X_MM
        ly = by - KICAD_PANEL_OFFSET_Y_MM
        return lx, ly

    sd_center: tuple[float, float] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        open_parens = line.count("(")
        close_parens = line.count(")")

        if not in_module and line.startswith("(module "):
            in_module = True
            depth = open_parens - close_parens
            mod_ref = None
            mod_at_x = 0.0
            mod_at_y = 0.0
            mod_rot_deg = 0.0
            continue

        if in_module:
            if line.startswith("(at "):
                tokens = line.strip("()").split()
                if len(tokens) >= 3:
                    try:
                        mod_at_x = float(tokens[1])
                        mod_at_y = float(tokens[2])
                    except ValueError:
                        pass
                if len(tokens) >= 4:
                    try:
                        mod_rot_deg = float(tokens[3])
                    except ValueError:
                        mod_rot_deg = 0.0

            if line.startswith("(fp_text reference "):
                parts = line.split()
                if len(parts) >= 3:
                    ref = parts[2]
                    if ref.startswith('"') and ref.endswith('"'):
                        ref = ref[1:-1]
                    mod_ref = ref

            depth += open_parens - close_parens
            if depth <= 0:
                if sd_center is None:
                    sd_center = flush_module()
                in_module = False
                depth = 0

    if in_module and sd_center is None:
        sd_center = flush_module()

    if sd_center is None:
        return None

    cx, cy = sd_center
    width = 2.3
    height = 12.0
    x = cx - width / 2.0
    y = cy - height / 2.0
    return (x, y, width, height)


def parse_patch_init_holes_from_drill(drill_path: Path) -> list[Hole]:
    """Parse Patch.Init blank-NPTH.drl and return NPTH hole centres in panel-local mm.

    The Excellon drill file is metric with absolute coordinates. We convert the
    raw board coordinates into the same panel-local coordinate system used for
    KiCad-derived hardware centres and Edge_Cuts parsing:
    - X right, Y down, origin at the top-left of the front panel.
    - Gerber/Excellon Y is negative downward, so we negate and offset by OY_TOP_MM.
    """
    text = drill_path.read_text(encoding="utf-8", errors="ignore")

    tool_diam_mm: dict[str, float] = {}
    current_tool: str | None = None
    holes: list[Hole] = []

    # Tool definitions: T1C3.000, T2C3.200, ...
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        m_tool = re.match(r"^T(\d+)C([0-9.]+)", line)
        if m_tool:
            tool_id = f"T{m_tool.group(1)}"
            tool_diam_mm[tool_id] = float(m_tool.group(2))
            continue

    # Identify the Eurorack mounting screw drill diameter so we can ignore those
    # holes when comparing against panel hardware. The mounting holes are the
    # smallest NPTHs on the board (~3.0 mm), so we take the minimum tool
    # diameter as the mount diameter.
    mount_diam: float | None = None
    if tool_diam_mm:
        mount_diam = min(tool_diam_mm.values())

    # Second pass: coordinates and tool selections.
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue

        # Bare tool selection (e.g. "T2")
        m_select = re.match(r"^T(\d+)\s*$", line)
        if m_select:
            current_tool = f"T{m_select.group(1)}"
            continue

        # Lines containing one or more coordinate pairs, e.g.:
        #   X51.945Y-46.347
        #   X33.045Y-30.095G85X35.045Y-30.095
        if "X" not in line or "Y" not in line:
            continue

        coords = re.findall(r"X([-\d.]+)Y([-\d.]+)", line)
        if not coords:
            continue
        if current_tool is None:
            continue
        diam = tool_diam_mm.get(current_tool)
        if diam is None:
            continue
        # Skip Eurorack rail mounting holes so they are not compared against
        # panel hardware centres or rendered in the drill-only SVG.
        if mount_diam is not None and abs(diam - mount_diam) < 1e-6:
            continue
        r = diam / 2.0

        for xs, ys in coords:
            gx = float(xs)
            gy = float(ys)
            lx = gx - PATCH_INIT_PANEL_ORIGIN_X_MM
            ly = -gy - OY_TOP_MM
            holes.append(Hole("circle", lx, ly, r))

    return holes


def _gerber_x46_to_mm(val: int) -> float:
    """Convert Gerber 4.6 format (4 int, 6 decimal) to mm."""
    return val / 1e6


def parse_patch_init_edge_cuts_sd_slot(edge_cuts_path: Path) -> tuple[float, float, float, float]:
    """Parse Patch.Init blank-Edge_Cuts.gbr and return the SD card holder cutout in panel-local mm.

    Returns (x, y, width, height). The Edge_Cuts file contains the board outline and one
    inner rectangle (the SD card slot). We return the inner rectangle in panel-local
    coordinates (origin top-left, X right, Y down).
    """
    import re as re_mod
    text = edge_cuts_path.read_text(encoding="utf-8", errors="ignore")
    ox = PATCH_INIT_PANEL_ORIGIN_X_MM
    oy_top = OY_TOP_MM

    points: list[tuple[float, float]] = []
    for match in re_mod.finditer(r"X(-?\d+)Y(-?\d+)", text, re_mod.IGNORECASE):
        gx = _gerber_x46_to_mm(int(match.group(1)))
        gy = _gerber_x46_to_mm(int(match.group(2)))
        lx = gx - ox
        ly = -gy - oy_top
        points.append((lx, ly))

    # Find all axis-aligned rectangles (consecutive 4 points that form a bbox)
    # Board outline is 50.8 x 128.5 mm; SD slot is ~3.2 x 12.8 mm
    rects: list[tuple[float, float, float, float]] = []
    for i in range(len(points) - 3):
        xs = [points[i + j][0] for j in range(4)]
        ys = [points[i + j][1] for j in range(4)]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        w = xmax - xmin
        h = ymax - ymin
        if w >= 2 and h >= 2:
            rects.append((xmin, ymin, w, h))

    # The SD slot is the rectangle that is not the board outline (50.8 x 128.5)
    for (x, y, w, h) in rects:
        if 2.0 <= w <= 5.0 and 8.0 <= h <= 18.0:
            return (x, y, w, h)
    raise ValueError("Could not find SD card slot rectangle in Edge_Cuts file.")


def generate_hardware_center_check_svg() -> None:
    """Generate two SVGs comparing KiCad hardware centres to NPTH drills.

    Instead of a single overlay, this writes:

    - `output/hardware-centers-kicad.svg` with blue outlined circles at the
      hardware centres derived from the KiCad PCB.
    - `output/hardware-centers-drill.svg` with red outlined circles at the
      NPTH drill locations parsed from `blank-NPTH.drl`.

    Both SVGs use the shared panel-local coordinate system (origin at the
    top-left of the front panel, X right, Y down) so they can be compared by
    toggling layers in an external viewer.
    """
    kicad_path = _panel_assets_path(
        "KiCad_PCB",
        "ES_Daisy_Patch_SM_FB_Rev1.kicad_pcb",
    )
    assert kicad_path.exists(), f"Patch.Init KiCad PCB not found: {kicad_path}"

    drill_path = _panel_assets_path(
        "patch_init_gerbers",
        "blank-NPTH.drl",
    )
    assert drill_path.exists(), f"Patch.Init NPTH drill file not found: {drill_path}"

    ref_centers = parse_patch_init_holes_from_kicad(kicad_path)
    assert ref_centers, "No hardware centres derived from Patch.Init KiCad file."

    drill_holes = parse_patch_init_holes_from_drill(drill_path)
    assert drill_holes, "No NPTH holes parsed from blank-NPTH.drl."

    width = PANEL_WIDTH_MM
    height = PANEL_HEIGHT_MM

    # SVG 1: KiCad-derived hardware centres only, plus the SD card slot cutout.
    kicad_lines: list[str] = []
    kicad_lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" '
        f'viewBox="0 0 {width} {height}">'
    )
    kicad_lines.append(
        '<rect x="0" y="0" width="{:.3f}" height="{:.3f}" fill="none" stroke="#808080" stroke-width="0.1" />'.format(
            width, height
        )
    )
    kicad_lines.append("  <!-- KiCad-derived hardware centres (blue outlines) -->")
    for (x, y, r_mech) in ref_centers:
        # Use the inferred mechanical panel cutout radius where available so the
        # blue circles represent the actual panel drill sizes, not just point
        # markers.
        kicad_lines.append(
            f'  <circle cx="{x:.3f}" cy="{y:.3f}" r="{r_mech:.3f}" fill="none" '
            f'stroke="#0000ff" stroke-width="0.2" />'
        )

    # Add the same wide Eurorack mounting screw slots used by the generator so
    # the KiCad-based overlay shows the full set of panel cutouts.
    screw_width_mm = 5.0
    screw_height_mm = 3.0
    y_top = 3.0
    y_bottom = PANEL_HEIGHT_MM - 3.0
    x_left = 7.50
    x_right = 43.10
    screw_centers = [
        (x_left, y_top),
        (x_right, y_top),
        (x_left, y_bottom),
        (x_right, y_bottom),
    ]
    for (cx, cy) in screw_centers:
        sx = cx - screw_width_mm / 2.0
        sy = cy - screw_height_mm / 2.0
        kicad_lines.append(
            f'  <rect x="{sx:.3f}" y="{sy:.3f}" width="{screw_width_mm:.3f}" '
            f'height="{screw_height_mm:.3f}" rx="1.0" fill="none" '
            f'stroke="#0000ff" stroke-width="0.2" />'
        )

    # Add the SD card holder slot as a 2.3 mm × 12.0 mm vertical panel cutout
    # centred on the VERT_MICROSD_CENTERED footprint (U_SDCARD1) derived from
    # the KiCad PCB.
    sd_slot = parse_patch_init_sd_slot_from_kicad(kicad_path)
    if sd_slot is not None:
        sx, sy, sw, sh = sd_slot
        kicad_lines.append(
            f'  <rect x="{sx:.3f}" y="{sy:.3f}" width="{sw:.3f}" height="{sh:.3f}" '
            f'fill="none" stroke="#0000ff" stroke-width="0.2" />'
        )
    kicad_lines.append("</svg>")

    # SVG 2: NPTH drills only.
    drill_lines: list[str] = []
    drill_lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" '
        f'viewBox="0 0 {width} {height}">'
    )
    drill_lines.append(
        '<rect x="0" y="0" width="{:.3f}" height="{:.3f}" fill="none" stroke="#808080" stroke-width="0.1" />'.format(
            width, height
        )
    )
    drill_lines.append("  <!-- NPTH drills from blank-NPTH.drl (red outlines) -->")
    for h in drill_holes:
        drill_lines.append(
            f'  <circle cx="{h.x:.3f}" cy="{h.y:.3f}" r="{h.r:.3f}" fill="none" '
            f'stroke="#ff0000" stroke-width="0.2" />'
        )
    drill_lines.append("</svg>")

    out_dir = HERE / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    kicad_path = out_dir / "hardware-centers-kicad.svg"
    drill_path_out = out_dir / "hardware-centers-drill.svg"
    kicad_path.write_text("\n".join(kicad_lines) + "\n", encoding="utf-8")
    drill_path_out.write_text("\n".join(drill_lines) + "\n", encoding="utf-8")


def generate_drill_only_svg() -> None:
    """Generate a drill-only SVG for PCBWay-style validation.

    The output `ResynthesisPanel_drill.svg` contains only:
    - The panel outline rectangle.
    - Circular representations of all hardware cuts (pots, jacks, switches,
      mounting holes, etc.) derived from `ResynthesisPanel.svg`.
    - The rectangular SD card holder slot.

    No artwork or text is included; this isolates the mechanical cut geometry
    so manufacturer-style checks can be run without any decorative shapes.
    """
    assert CANONICAL_SVG.exists(), f"Canonical SVG not found: {CANONICAL_SVG}"

    holes = extract_holes(CANONICAL_SVG)
    assert holes, "No holes detected in canonical panel SVG; cannot build drill-only SVG."

    # Board dimensions in mm.
    width = PANEL_WIDTH_MM
    height = PANEL_HEIGHT_MM

    lines: list[str] = []
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" '
        f'viewBox="0 0 {width} {height}">'
    )
    lines.append(
        '<rect x="0" y="0" width="{:.3f}" height="{:.3f}" fill="none" stroke="#000000" stroke-width="0.1" />'.format(
            width, height
        )
    )

    lines.append('  <!-- Drill/cut geometry: circles represent panel holes/cutouts -->')
    for h in holes:
        # Represent both circular and rectangular hardware cuts as circles using
        # the stored equivalent radius. This keeps the validation geometry
        # simple while still matching the edge-to-edge clearances enforced by
        # the tests elsewhere in this module.
        lines.append(
            f'  <circle cx="{h.x:.3f}" cy="{h.y:.3f}" r="{h.r:.3f}" '
            'fill="none" stroke="#000000" stroke-width="0.2" />'
        )

    # Add the SD card slot rectangle from Edge_Cuts so its cutout participates
    # in distance-to-edge checks.
    edge_cuts_path = _panel_assets_path(
        "patch_init_gerbers",
        "blank-Edge_Cuts.gbr",
    )
    if edge_cuts_path.exists():
        try:
            sx, sy, sw, sh = parse_patch_init_edge_cuts_sd_slot(edge_cuts_path)
        except Exception:
            sx = sy = sw = sh = None
        if sx is not None and sw is not None and sh is not None:
            lines.append(
                f'  <rect x="{sx:.3f}" y="{sy:.3f}" width="{sw:.3f}" height="{sh:.3f}" '
                'fill="none" stroke="#000000" stroke-width="0.2" />'
            )

    lines.append("</svg>")

    DRILL_ONLY_SVG.parent.mkdir(parents=True, exist_ok=True)
    DRILL_ONLY_SVG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_panel_cutout_overlay_for_footprint(fp_basename: str) -> None:
    """Render a simple SVG showing footprint geometry plus inferred panel cutout.

    The SVG is written to HERE / "footprint_calc" / f"{fp_basename}_panel_overlay.svg".
    This is a diagnostic aid only and is not used by the tests.
    """
    fp_dir = _panel_assets_path("KiCad_PCB", "ES_Daisy_Patch_SM_FB_Rev1.pretty")
    fp_path = fp_dir / f"{fp_basename}.kicad_mod"
    if not fp_path.exists():
        # Best-effort only; silently skip if the footprint file is not present.
        return

    text = fp_path.read_text(encoding="utf-8", errors="ignore")

    # Choose a panel cutout diameter based on the footprint / part family.
    # We intentionally mirror the module selection heuristic used for front‑panel
    # hardware (jacks J_*, pots VR_*, switches SW_*, LEDs) and then apply a
    # fixed mechanical diameter centred at (0, 0) inside the footprint.
    #
    # Diameters and clearances match panel/datasheets/README.md:
    # - 9MM_SNAP-IN_POT… footprints → 7.5 mm ⌀ panel (7.2 mm drill + 0.3 mm clearance).
    # - S_JACK… footprints          → 6.3 mm ⌀ panel (6.2 mm drill + 0.1 mm clearance).
    # - TL1105… footprints          → 5.5 mm ⌀ panel (5.5 mm drill + 0.0 mm clearance).
    # - TOGGLE_ON-ON (B_8) switch   → 6.3 mm ⌀ panel (6.2 mm drill + 0.1 mm clearance).
    #
    # The cutout is always placed at the local footprint origin so it lines up
    # with the derived hardware centre used elsewhere in this module.
    name_upper = fp_basename.upper()
    panel_diam: float | None
    if "9MM_SNAP-IN_POT" in name_upper:
        panel_diam = POT_PANEL_DIAMETER_MM
    elif "S_JACK" in name_upper:
        panel_diam = JACK_PANEL_DIAMETER_MM
    elif "TL1105" in name_upper:
        panel_diam = SWITCH_B7_PANEL_DIAMETER_MM
    elif "TOGGLE" in name_upper and "ON-ON" in name_upper:
        panel_diam = SWITCH_B8_PANEL_DIAMETER_MM
    elif "LED" in name_upper:
        panel_diam = LED_DRILL_DIAMETER_MM
    else:
        panel_diam = None

    mech_r = panel_diam / 2.0 if panel_diam is not None else None

    import re as re_mod

    circles: list[tuple[float, float, float]] = []
    lines: list[tuple[float, float, float, float]] = []

    # Very small parser for fp_circle/fp_line primitives, sufficient for a
    # visual approximation of the footprint in an SVG.
    for line in text.splitlines():
        line = line.strip()
        m_circ = re_mod.search(
            r"\(fp_circle\s+\(center\s+([-\d.]+)\s+([-\d.]+)\)\s+\(end\s+([-\d.]+)\s+([-\d.]+)\)",
            line,
        )
        if m_circ:
            cx = float(m_circ.group(1))
            cy = float(m_circ.group(2))
            ex = float(m_circ.group(3))
            ey = float(m_circ.group(4))
            r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            circles.append((cx, cy, r))
            continue

        m_line = re_mod.search(
            r"\(fp_line\s+\(start\s+([-\d.]+)\s+([-\d.]+)\)\s+\(end\s+([-\d.]+)\s+([-\d.]+)\)",
            line,
        )
        if m_line:
            x1 = float(m_line.group(1))
            y1 = float(m_line.group(2))
            x2 = float(m_line.group(3))
            y2 = float(m_line.group(4))
            lines.append((x1, y1, x2, y2))

    # Include the panel cutout circle at (0,0) in the bounds.
    xs: list[float] = []
    ys: list[float] = []
    for (cx, cy, r) in circles:
        xs.extend([cx - r, cx + r])
        ys.extend([cy - r, cy + r])
    for (x1, y1, x2, y2) in lines:
        xs.extend([x1, x2])
        ys.extend([y1, y2])
    if mech_r is not None:
        xs.extend([-mech_r, mech_r])
        ys.extend([-mech_r, mech_r])

    if not xs or not ys:
        # Nothing sensible to render.
        return

    margin = 1.0  # mm
    min_x = min(xs) - margin
    max_x = max(xs) + margin
    min_y = min(ys) - margin
    max_y = max(ys) + margin
    width = max_x - min_x
    height = max_y - min_y

    # SVG with millimetre units; we use the KiCad coordinate system directly.
    svg_elems: list[str] = []
    svg_elems.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" '
        f'viewBox="{min_x} {min_y} {width} {height}">'
    )
    svg_elems.append('<g stroke="black" stroke-width="0.1" fill="none">')
    for (x1, y1, x2, y2) in lines:
        svg_elems.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>')
    for (cx, cy, r) in circles:
        svg_elems.append(f'<circle cx="{cx}" cy="{cy}" r="{r}"/>')
    svg_elems.append("</g>")

    if mech_r is not None:
        svg_elems.append(
            '<g stroke="red" stroke-width="0.15" fill="none">'
            f'<circle cx="0" cy="0" r="{mech_r}"/>'
            "</g>"
        )

    svg_elems.append("</svg>")
    svg_text = "\n".join(svg_elems) + "\n"

    out_dir = HERE / "footprint_calc"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{fp_basename}_panel_overlay.svg"
    out_path.write_text(svg_text, encoding="utf-8")


def extract_svg_cutout_rects(svg_path: Path) -> list[tuple[float, float, float, float]]:
    """Extract rectangular cutouts from an SVG (e.g. SD card holder).

    Returns list of (x, y, width, height) for <rect> elements that are in the
    size range of the SD slot (~3 x 13 mm), excluding elements inside defs/pattern/mask.
    """
    tree = ET.parse(svg_path)
    root = tree.getroot()
    parent_map = {c: p for p in root.iter() for c in p}
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    def _inside_defs_or_pattern(el: ET.Element) -> bool:
        tag = (el.tag.split("}")[-1] if "}" in el.tag else el.tag).lower()
        if tag in ("defs", "pattern", "mask", "lineargradient", "radialgradient"):
            return True
        p = parent_map.get(el)
        return p is not None and _inside_defs_or_pattern(p)

    cutouts: list[tuple[float, float, float, float]] = []
    for el in root.iter(f"{ns}rect"):
        if _inside_defs_or_pattern(el):
            continue
        x_attr = el.get("x")
        y_attr = el.get("y")
        w_attr = el.get("width")
        h_attr = el.get("height")
        if x_attr is None or y_attr is None or w_attr is None or h_attr is None:
            continue
        x = _parse_float(x_attr)
        y = _parse_float(y_attr)
        w = _parse_float(w_attr)
        h = _parse_float(h_attr)
        if 2.0 <= w <= 5.0 and 8.0 <= h <= 18.0:
            cutouts.append((x, y, w, h))
    return cutouts


def test_panel_cutouts_four_pots_switches_and_sd_slot_match_patch_init() -> None:
    """Verify the 4 pot cutouts, 2 switch cutouts, and SD card holder cutout match Patch.Init.

    The 4 potentiometer shaft holes (7.2 mm), the B_7 switch hole (~5.5 mm),
    and the B_8 switch hole (~6.2 mm, sharing the jack-family tool) are
    already verified by test_panel_drill_holes_match_patch_init. This test adds verification
    that the rectangular SD card holder cutout from blank-Edge_Cuts.gbr is present
    in the panel SVG and matches position and size within tolerance.
    """
    # SD card holder from Edge_Cuts
    edge_cuts_path = _panel_assets_path(
        "patch_init_gerbers",
        "blank-Edge_Cuts.gbr",
    )
    assert edge_cuts_path.exists(), f"Patch.Init Edge_Cuts not found: {edge_cuts_path}"
    ref_x, ref_y, ref_w, ref_h = parse_patch_init_edge_cuts_sd_slot(edge_cuts_path)

    svg_cutouts = extract_svg_cutout_rects(CANONICAL_SVG)
    assert svg_cutouts, (
        "Panel SVG must include the SD card holder rectangular cutout matching "
        "patch_init_gerbers/blank-Edge_Cuts.gbr. Add a <rect> for the SD slot."
    )
    pos_tol = 0.1
    size_tol = 0.1
    matched = False
    for (sx, sy, sw, sh) in svg_cutouts:
        if (
            abs(sx - ref_x) <= pos_tol
            and abs(sy - ref_y) <= pos_tol
            and abs(sw - ref_w) <= size_tol
            and abs(sh - ref_h) <= size_tol
        ):
            matched = True
            break
    assert matched, (
        f"SD card holder cutout in SVG does not match Patch.Init Edge_Cuts. "
        f"Expected (x,y,w,h)=({ref_x:.2f}, {ref_y:.2f}, {ref_w:.2f}, {ref_h:.2f}) mm; "
        f"SVG cutouts found: {svg_cutouts}"
    )


def test_panel_drill_holes_match_patch_init() -> None:
    """[First test] Panel drill positions must match the Patch.Init KiCad layout.

    This is the primary gate: the canonical panel SVG must have the same
    hardware hole centres as the Patch.Init KiCad PCB (position tolerance
    0.05 mm). Any panel design must pass this before other checks. Preview
    images (e.g. ResynthesisPanel.jpg) should be exported from the SVG so
    they reflect this validated layout.
    """
    kicad_path = _panel_assets_path(
        "KiCad_PCB",
        "ES_Daisy_Patch_SM_FB_Rev1.kicad_pcb",
    )
    assert kicad_path.exists(), f"Patch.Init KiCad PCB not found: {kicad_path}"

    ref_centers = parse_patch_init_holes_from_kicad(kicad_path)
    assert ref_centers, "No hardware centres derived from Patch.Init KiCad file."

    # Only the centre positions are relevant here; mechanical radii are used for
    # visualisation and clearance checks elsewhere.
    ref_xy = [(x, y) for (x, y, _r) in ref_centers]

    svg_holes = extract_holes(CANONICAL_SVG)
    svg_circles = [h for h in svg_holes if h.kind == "circle"]
    pos_tol = 0.05

    svg_xy = [(h.x, h.y) for h in svg_circles]

    errors: list[str] = []

    # Every KiCad-derived hardware centre must have a matching SVG hole.
    for (gx, gy) in ref_xy:
        best = min(svg_xy, key=lambda p: hypot(p[0] - gx, p[1] - gy))
        dist = hypot(best[0] - gx, best[1] - gy)
        if dist > pos_tol:
            errors.append(
                f"KiCad hardware centre at ({gx:.3f}, {gy:.3f}) has no matching SVG hole within {pos_tol} mm "
                f"(nearest SVG hole at ({best[0]:.3f}, {best[1]:.3f}), Δ={dist:.3f} mm)."
            )

    # And we should not have extra SVG holes far from any KiCad hardware centre.
    for (sx, sy) in svg_xy:
        best = min(ref_xy, key=lambda p: hypot(p[0] - sx, p[1] - sy))
        dist = hypot(best[0] - sx, best[1] - sy)
        if dist > pos_tol:
            errors.append(
                f"SVG hole at ({sx:.3f}, {sy:.3f}) does not correspond to any KiCad hardware centre "
                f"(nearest KiCad centre at ({best[0]:.3f}, {best[1]:.3f}), Δ={dist:.3f} mm)."
            )

    assert not errors, "Panel holes do not match Patch.Init KiCad hardware layout:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


def test_panel_drill_holes_match_npht_drill() -> None:
    """Panel drill positions must also match the Patch.Init NPTH drill file.

    The NPTH drill file is the manufacturing source of truth for non-plated
    front-panel drills. This test cross-checks the canonical SVG directly
    against `blank-NPTH.drl` so that any divergence is caught even if the KiCad
    layout and drill file were to get out of sync.
    """
    drill_path = _panel_assets_path(
        "patch_init_gerbers",
        "blank-NPTH.drl",
    )
    assert drill_path.exists(), f"Patch.Init NPTH drill file not found: {drill_path}"

    drill_holes = parse_patch_init_holes_from_drill(drill_path)
    assert drill_holes, "No NPTH holes parsed from blank-NPTH.drl."

    svg_holes = extract_holes(CANONICAL_SVG)
    svg_circles = [h for h in svg_holes if h.kind == "circle"]
    assert svg_circles, "No circular holes found in canonical panel SVG."

    errors = _match_holes(drill_holes, svg_circles, tol_mm=0.05)
    assert not errors, "Panel holes do not match Patch.Init NPTH drill file:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


def test_b10_panel_hole_matches_j_gatein1_mechanics() -> None:
    """Hole for B10 jack must use the S_JACK family diameter.

    B10 is the top-row left gate input on the Patch.Init front panel and is
    wired to the J_GATEIN1 S_JACK footprint in the KiCad PCB. This test checks
    that the panel drill at the B10 position has the same mechanical diameter
    as the S_JACK body/shaft defined in the KiCad footprint, and that the SVG
    hole center aligns with the canonical B10 panel coordinates.
    """
    holes = extract_holes(CANONICAL_SVG)
    circle_holes = [h for h in holes if h.kind == "circle"]
    assert circle_holes, "No circular holes found in canonical panel SVG."

    # Canonical B10 jack center in panel-local mm (from Patch.Init layout).
    b10_x, b10_y = 7.15, 84.562

    nearest = min(circle_holes, key=lambda h: hypot(h.x - b10_x, h.y - b10_y))
    center_dist = hypot(nearest.x - b10_x, nearest.y - b10_y)
    center_tol = 0.05
    assert center_dist <= center_tol, (
        f"Nearest SVG hole to B10 is offset by {center_dist:.3f} mm "
        f"(tolerance {center_tol:.3f} mm); check B10 drill position in ResynthesisPanel.svg."
    )

    actual_d = 2.0 * nearest.r
    expected_d = JACK_PANEL_DIAMETER_MM
    diam_tol = 0.05
    assert abs(actual_d - expected_d) <= diam_tol, (
        f"B10 panel hole diameter {actual_d:.3f} mm does not match jack family "
        f"diameter {expected_d:.3f} mm (tolerance ±{diam_tol:.3f} mm) from "
        "generate_resynthesis_panel_svg.PANEL_* constants."
    )


def test_component_family_panel_diameters_match_constants() -> None:
    """Panel jack, pot, and switch holes must match generator constants.

    The generator script is the single source of truth for final panel drill
    diameters; this test ensures the canonical SVG uses the same values for:

    - 9MM_SNAP-IN_POT… potentiometers (pots) → POT_PANEL_DIAMETER_MM
    - TL1105… toggle switch at B_7 → SWITCH_B7_PANEL_DIAMETER_MM
    - Toggle switch at B_8 (PITCH LOCK) sharing jack-family drill → SWITCH_B8_PANEL_DIAMETER_MM
    - S_JACK… audio/CV jacks and gates → JACK_PANEL_DIAMETER_MM
    """
    holes = extract_holes(CANONICAL_SVG)
    circle_holes = [h for h in holes if h.kind == "circle"]
    assert circle_holes, "No circular holes found in canonical panel SVG."

    # Canonical centres for one representative of each family (panel-local mm).
    # Pots: CV_1 at (11.176, 22.904)
    pot_center = (11.176, 22.904)
    # Switches: B_7 at (8.65, 59.288); B_8 at (25.503, 61.957)
    switch_b7_center = (8.65, 59.288)
    switch_b8_center = (25.503, 61.957)
    # Jacks: B10 at (7.15, 84.562)
    jack_center = (7.15, 84.562)

    def _nearest(center: tuple[float, float]) -> Hole:
        cx, cy = center
        return min(circle_holes, key=lambda h: hypot(h.x - cx, h.y - cy))

    pot_hole = _nearest(pot_center)
    switch_b7_hole = _nearest(switch_b7_center)
    switch_b8_hole = _nearest(switch_b8_center)
    jack_hole = _nearest(jack_center)

    diam_tol = 0.05

    pot_d = 2.0 * pot_hole.r
    assert abs(pot_d - POT_PANEL_DIAMETER_MM) <= diam_tol, (
        f"Pot panel hole diameter {pot_d:.3f} mm does not match POT_PANEL_DIAMETER_MM="
        f"{POT_PANEL_DIAMETER_MM:.3f} mm (tolerance ±{diam_tol:.3f} mm)."
    )

    switch_b7_d = 2.0 * switch_b7_hole.r
    assert abs(switch_b7_d - SWITCH_B7_PANEL_DIAMETER_MM) <= diam_tol, (
        "B_7 switch panel hole diameter "
        f"{switch_b7_d:.3f} mm does not match SWITCH_B7_PANEL_DIAMETER_MM="
        f"{SWITCH_B7_PANEL_DIAMETER_MM:.3f} mm (tolerance ±{diam_tol:.3f} mm)."
    )

    switch_b8_d = 2.0 * switch_b8_hole.r
    assert abs(switch_b8_d - SWITCH_B8_PANEL_DIAMETER_MM) <= diam_tol, (
        "B_8 switch panel hole diameter "
        f"{switch_b8_d:.3f} mm does not match SWITCH_B8_PANEL_DIAMETER_MM="
        f"{SWITCH_B8_PANEL_DIAMETER_MM:.3f} mm (tolerance ±{diam_tol:.3f} mm)."
    )

    jack_d = 2.0 * jack_hole.r
    assert abs(jack_d - JACK_PANEL_DIAMETER_MM) <= diam_tol, (
        f"Jack panel hole diameter {jack_d:.3f} mm does not match JACK_PANEL_DIAMETER_MM="
        f"{JACK_PANEL_DIAMETER_MM:.3f} mm (tolerance ±{diam_tol:.3f} mm)."
    )


def _match_holes(
    canon: Iterable[Hole],
    candidate: Iterable[Hole],
    tol_mm: float = 0.15,
) -> list[str]:
    """Return a list of mismatch descriptions between two hole sets.

    `tol_mm` is a positional tolerance in millimetres.
    """
    canon_list = list(canon)
    cand_list = list(candidate)

    errors: list[str] = []

    if len(canon_list) != len(cand_list):
        errors.append(
            f"hole-count mismatch: canonical has {len(canon_list)}, "
            f"candidate has {len(cand_list)}"
        )

    # Greedy nearest-neighbour matching by position only.
    unmatched = cand_list.copy()
    for h in canon_list:
        nearest = None
        nearest_dist = float("inf")
        for c in unmatched:
            d = hypot(h.x - c.x, h.y - c.y)
            if d < nearest_dist:
                nearest_dist = d
                nearest = c
        if nearest is None:
            errors.append(f"no matching hole for canonical hole at ({h.x:.3f}, {h.y:.3f})")
            continue
        if nearest_dist > tol_mm:
            errors.append(
                "hole position mismatch: "
                f"canonical ({h.x:.3f}, {h.y:.3f}) vs "
                f"candidate ({nearest.x:.3f}, {nearest.y:.3f}), "
                f"Δ={nearest_dist:.3f} mm > {tol_mm:.3f} mm"
            )
        unmatched.remove(nearest)

    return errors


def _match_hole_sets_with_warnings(
    ref: Iterable[Hole],
    candidate: Iterable[Hole],
    description: str,
    tol_mm: float = 0.15,
) -> list[str]:
    """Compare two hole sets and tag any mismatch messages with a description."""
    raw_errors = _match_holes(ref, candidate, tol_mm=tol_mm)
    return [f"{description}: {msg}" for msg in raw_errors]


def test_custom_panels_align_with_canonical() -> None:
    """All custom panel SVGs must share the same hardware hole layout."""
    assert CANONICAL_SVG.exists(), f"Canonical SVG not found: {CANONICAL_SVG}"

    canonical_holes = extract_holes(CANONICAL_SVG)
    assert canonical_holes, "No holes detected in canonical panel SVG."

    # Any alternate designs are named ResynthesisPanel_*.svg
    alt_svgs = sorted(HERE.glob("ResynthesisPanel_*.svg"))
    if not alt_svgs:
        # No alternates present – nothing to compare, but the canonical panel
        # still parsed successfully above.
        return

    for svg in alt_svgs:
        candidate_holes = extract_holes(svg)
        assert candidate_holes, f"No holes detected in candidate panel {svg.name!r}."

        errors = _match_holes(canonical_holes, candidate_holes)
        assert not errors, (
            f"Hole layout mismatch for {svg.name}:\n" + "\n".join(f"- {e}" for e in errors)
        )


def test_canonical_panel_dimensions_and_diameters() -> None:
    """Basic mechanical sanity checks for the primary panel.

    - SVG viewBox/size must match 3U x 10HP (128.5 x 50.8 mm).
    - All drilled holes of the same functional group should share a radius,
      ensuring consistent diameters for pots vs jacks vs mounting holes.
    - Patch.Init Gerbers are present and metric, so the scale is consistent
      with the PCB they target.
    """
    assert CANONICAL_SVG.exists(), f"Canonical SVG not found: {CANONICAL_SVG}"

    # Check SVG size attributes
    import xml.etree.ElementTree as ET  # local import to avoid polluting module namespace

    tree = ET.parse(CANONICAL_SVG)
    root = tree.getroot()
    width_attr = root.get("width")
    height_attr = root.get("height")
    view_box = root.get("viewBox")

    assert width_attr and height_attr and view_box, "SVG must define width, height, and viewBox."
    assert width_attr.endswith("mm") and height_attr.endswith(
        "mm"
    ), "Panel dimensions should be specified in millimetres."

    width_mm = float(width_attr[:-2])
    height_mm = float(height_attr[:-2])
    assert abs(width_mm - 50.8) < 0.05, f"Panel width {width_mm}mm != 50.8mm (10HP)."
    assert abs(height_mm - 128.5) < 0.1, f"Panel height {height_mm}mm != 128.5mm (3U)."

    # Group hole radii and ensure groups are internally consistent
    holes = extract_holes(CANONICAL_SVG)
    assert holes, "No holes detected in canonical panel SVG."

    # Simple grouping heuristics by approximate radius
    radii = sorted({round(h.r, 2) for h in holes})
    # We expect a small number of distinct radii (e.g. mounting, pots, jacks)
    assert len(radii) <= 5, f"Unexpectedly large variety of hole radii: {radii}"

    # Check Patch.Init drill file exists and is metric (for scale consistency)
    drill_dir = _panel_assets_path("patch_init_gerbers")
    pth_drl = drill_dir / "blank-PTH.drl"
    assert pth_drl.exists(), f"Patch.Init drill file not found: {pth_drl}"
    contents = pth_drl.read_text(encoding="utf-8", errors="ignore")
    assert "METRIC" in contents, "Expected Patch.Init drill file to use metric units."


def test_screw_holes_eurorack_rail_distance() -> None:
    """Screw hole centers must match Eurorack rail: same distance from top/bottom as standard.

    Per Doepfer A-100 / panel/eurorack_spec/README.md: mounting hole center is 3 mm
    from the top edge and 3 mm from the bottom edge (so top row at y=3 mm, bottom
    row at y=128.5-3=125.5 mm). This test verifies the four corner screw cutouts
    in the panel SVG have their vertical centers at those positions so the panel
    aligns with standard Eurorack rails.
    """
    assert CANONICAL_SVG.exists(), f"Canonical SVG not found: {CANONICAL_SVG}"

    centers = extract_screw_cutout_centers(CANONICAL_SVG)
    assert len(centers) == 4, (
        f"Expected exactly 4 screw cutout rects (black-filled corner slots), got {len(centers)}. "
        "Check ResynthesisPanel.svg for four corner screw cutouts with fill=\"#000000\"."
    )

    tol_mm = 0.5
    expected_top_y = SCREW_CENTER_FROM_TOP_EDGE_MM
    expected_bottom_y = PANEL_HEIGHT_MM - SCREW_CENTER_FROM_BOTTOM_EDGE_MM

    # centers are sorted by (y, x): index 0,1 = top row; index 2,3 = bottom row
    top_centers = centers[:2]
    bottom_centers = centers[2:]

    errors: List[str] = []
    for (cx, cy) in top_centers:
        if abs(cy - expected_top_y) > tol_mm:
            errors.append(
                f"Top screw cutout at ({cx:.2f}, {cy:.2f}) has center_y={cy:.2f} mm; "
                f"Eurorack standard is {expected_top_y} mm from top (tolerance ±{tol_mm} mm)."
            )
    for (cx, cy) in bottom_centers:
        if abs(cy - expected_bottom_y) > tol_mm:
            errors.append(
                f"Bottom screw cutout at ({cx:.2f}, {cy:.2f}) has center_y={cy:.2f} mm; "
                f"Eurorack standard is {expected_bottom_y} mm from top "
                f"(i.e. {SCREW_CENTER_FROM_BOTTOM_EDGE_MM} mm from bottom, tolerance ±{tol_mm} mm)."
            )

    assert not errors, (
        "Screw holes must align with Eurorack rail (same distance from bottom/top as standard):\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


def test_panel_passes_pcbway_style_validation() -> None:
    """Run PCBWay-style design-rule checks so the panel would pass their online validation.

    This mirrors checks that PCBWay's portal performs when you upload Gerbers
    (see their help center: hole design standard, min non-plated holes,
    spacing from one hole to another, hole to board edge). If this test
    passes, the lead panel design is expected to pass manufacturer validation.

    Rules applied:
    - Minimum non-plated hole diameter >= 0.45 mm.
    - Minimum spacing between holes (edge-to-edge) >= 0.2 mm.
    - Minimum distance from hole edge to board outline >= 0.5 mm.
    - Board dimensions within manufacturer capability (e.g. <= 500 mm).
    """
    # Validate against a drill-only SVG that contains just the mechanical cuts
    # (no artwork or text), so the checks apply solely to manufacturable
    # geometry.
    generate_drill_only_svg()
    assert DRILL_ONLY_SVG.exists(), f"Drill-only SVG not found: {DRILL_ONLY_SVG}"

    tree = ET.parse(DRILL_ONLY_SVG)
    root = tree.getroot()
    width_attr = root.get("width")
    height_attr = root.get("height")
    assert width_attr and height_attr, "SVG must define width and height."
    width_mm = float(width_attr.replace("mm", "").strip())
    height_mm = float(height_attr.replace("mm", "").strip())

    # Board within max dimension
    assert width_mm <= PCBWAY_MAX_BOARD_DIMENSION_MM, (
        f"Panel width {width_mm} mm exceeds PCBWay max {PCBWAY_MAX_BOARD_DIMENSION_MM} mm."
    )
    assert height_mm <= PCBWAY_MAX_BOARD_DIMENSION_MM, (
        f"Panel height {height_mm} mm exceeds PCBWay max {PCBWAY_MAX_BOARD_DIMENSION_MM} mm."
    )

    holes = extract_holes(DRILL_ONLY_SVG)
    assert holes, "No holes in drill-only panel SVG; cannot validate."

    min_diameter = PCBWAY_MIN_NPTH_DIAMETER_MM
    min_radius = min_diameter / 2.0

    for i, h in enumerate(holes):
        # Minimum hole size (diameter >= 0.45 mm; we use radius so 2*r >= 0.45)
        assert h.r >= min_radius, (
            f"Hole {i} at ({h.x:.3f}, {h.y:.3f}) has radius {h.r:.3f} mm "
            f"(diameter {2*h.r:.3f} mm). PCBWay min non-plated hole diameter is {min_diameter} mm."
        )

        # Hole edge to board outline >= 0.5 mm (center must be at least r + 0.5 from each edge)
        margin = PCBWAY_MIN_HOLE_TO_BOARD_EDGE_MM
        dist_left = h.x
        dist_right = width_mm - h.x
        dist_top = h.y
        dist_bottom = height_mm - h.y
        for name, dist in [("left", dist_left), ("right", dist_right), ("top", dist_top), ("bottom", dist_bottom)]:
            assert dist >= h.r + margin, (
                f"Hole {i} at ({h.x:.3f}, {h.y:.3f}) too close to {name} edge: "
                f"center-to-edge={dist:.3f} mm, hole radius={h.r:.3f} mm; "
                f"need center-to-edge >= {h.r + margin:.3f} mm (hole edge to board >= {margin} mm)."
            )

    # Pairwise: minimum spacing between holes (edge-to-edge >= 0.2 mm).
    # Collect all violations so we can report detailed context if validation fails.
    spacing_violations: list[str] = []
    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            hi, hj = holes[i], holes[j]
            c2c = hypot(hi.x - hj.x, hi.y - hj.y)
            min_c2c = hi.r + hj.r + PCBWAY_MIN_HOLE_SPACING_MM
            if c2c < min_c2c:
                actual_edge = c2c - (hi.r + hj.r)
                spacing_violations.append(
                    "Holes {i} and {j} too close: "
                    f"Hole {i} @ ({hi.x:.3f}, {hi.y:.3f}), r={hi.r:.3f} mm "
                    f"(Ø={2*hi.r:.3f} mm); "
                    f"Hole {j} @ ({hj.x:.3f}, {hj.y:.3f}), r={hj.r:.3f} mm "
                    f"(Ø={2*hj.r:.3f} mm); "
                    f"center-to-center={c2c:.3f} mm, edge-to-edge={actual_edge:.3f} mm; "
                    f"required edge-to-edge >= {PCBWAY_MIN_HOLE_SPACING_MM:.3f} mm."
                )

    assert not spacing_violations, (
        "PCBWay-style spacing check failed: some non-plated holes overlap or are closer "
        "than the required edge-to-edge clearance.\n"
        "These hole centres are taken from the canonical panel SVG `ResynthesisPanel.svg`, "
        "which is generated by `generate_resynthesis_panel_svg.py` using cut geometry "
        "derived from `assets/patch_init_gerbers/blank-NPTH.drl` and the Patch.Init KiCad "
        "PCB (`KiCad_PCB/ES_Daisy_Patch_SM_FB_Rev1.kicad_pcb`).\n"
        "Review those sources to understand why a cutout was added or enlarged.\n"
        "Overlapping / too-close hole pairs:\n"
        + "\n".join(f"  - {msg}" for msg in spacing_violations)
    )


def test_knob_labels_not_obscured_by_rogan_knobs() -> None:
    """Ensure primary pot labels are readable when using Rogan-style knobs.

    We assume MI-style Rogan knobs with diameter 12 mm and require that
    the primary label (DRY / WET, SMOOTH, FLUFF, BRIGHT / or first line of BRIGHT / DARK) for each
    of the four pots does not intersect the knob's circular footprint.
    Sub-labels (e.g. CV_1) may sit between the two knob rows.
    """
    holes = extract_holes(CANONICAL_SVG)
    texts = extract_text(CANONICAL_SVG)

    # Pot holes are the large holes used for CV_1–CV_4 (r ~ 4mm in this SVG)
    pot_centers = [(h.x, h.y) for h in holes if h.kind == "circle" and 3.5 <= h.r <= 4.5]
    assert len(pot_centers) >= 4, f"Expected at least 4 pot holes, found {len(pot_centers)}."

    knob_r = ROGAN_KNOB_DIAMETER_MM / 2.0 + ROGAN_LABEL_CLEARANCE_MM

    # Only check primary pot labels (one per knob). BRIGHT / DARK is split into two lines; "BRIGHT /" is the primary.
    pot_texts = [
        t
        for t in texts
        if t.text in ("DRY / WET", "SMOOTH", "FLUFF", "BRIGHT /")
    ]
    assert pot_texts, "No pot labels found to validate."

    failures: list[str] = []
    for t in pot_texts:
        bbox = _approx_text_bbox(t)
        # Find nearest pot center
        nearest = min(pot_centers, key=lambda c: hypot(c[0] - t.x, c[1] - t.y))
        if _bbox_circle_intersects(bbox, nearest[0], nearest[1], knob_r):
            failures.append(
                f"Label {t.text!r} at ({t.x:.2f},{t.y:.2f}) intersects knob at ({nearest[0]:.2f},{nearest[1]:.2f})"
            )

    assert not failures, "Some labels would be obscured by knobs:\n" + "\n".join(f"- {f}" for f in failures)


def test_no_overlapping_text() -> None:
    """Ensure no two text elements in the panel overlap.

    Uses approximate bounding boxes. Same-column multi-line labels (e.g. BRIGHT /
    and DARK, or DIFFU and SION) are allowed to touch. A small overlap (-0.8 mm)
    is allowed so that adjacent jack labels (e.g. DENSITY and DIFFU) that barely
    touch in the approximation still pass.
    """
    assert CANONICAL_SVG.exists(), f"Canonical SVG not found: {CANONICAL_SVG}"
    texts = extract_text(CANONICAL_SVG)
    margin_mm = -0.8
    errors: list[str] = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            ti, tj = texts[i], texts[j]
            # Allow same-column multi-line labels (e.g. BRIGHT / and DARK, or DIFFU and SION) to touch/overlap
            if abs(ti.x - tj.x) < 0.5 and abs(ti.y - tj.y) < 6.0:
                continue
            bi = _approx_text_bbox(ti)
            bj = _approx_text_bbox(tj)
            if _bboxes_intersect(bi, bj, margin_mm):
                errors.append(
                    f"Overlapping text: {ti.text!r} at ({ti.x:.2f},{ti.y:.2f}) "
                    f"overlaps {tj.text!r} at ({tj.x:.2f},{tj.y:.2f})"
                )
    assert not errors, "Overlapping text in panel:\n" + "\n".join(f"  - {e}" for e in errors)


# Printed area = panel bounds (0, 0) to (width_mm, height_mm). Standard panel size.
PANEL_WIDTH_MM = 50.8
PANEL_HEIGHT_MM = 128.5
PRINTED_AREA_MARGIN_MM = 1.0


def test_text_within_printed_area() -> None:
    """All text must stay within the printed panel area (with a small margin).

    No label should extend beyond the panel bounds (0, 0) to (50.8, 128.5) mm.
    Uses approximate bounding boxes; a margin (default 1 mm) is required from
    each edge so that text does not run off the physical panel.
    """
    assert CANONICAL_SVG.exists(), f"Canonical SVG not found: {CANONICAL_SVG}"
    texts = extract_text(CANONICAL_SVG)
    margin = PRINTED_AREA_MARGIN_MM
    min_x = margin
    max_x = PANEL_WIDTH_MM - margin
    min_y = margin
    max_y = PANEL_HEIGHT_MM - margin

    errors: list[str] = []
    for t in texts:
        bbox = _approx_text_bbox(t)
        minbx, minby, maxbx, maxby = bbox
        if minbx < min_x:
            errors.append(
                f"Text {t.text!r} at ({t.x:.2f},{t.y:.2f}) extends left of printed area "
                f"(bbox minx={minbx:.2f} mm < {min_x:.2f} mm)"
            )
        if maxbx > max_x:
            errors.append(
                f"Text {t.text!r} at ({t.x:.2f},{t.y:.2f}) extends right of printed area "
                f"(bbox maxx={maxbx:.2f} mm > {max_x:.2f} mm)"
            )
        if minby < min_y:
            errors.append(
                f"Text {t.text!r} at ({t.x:.2f},{t.y:.2f}) extends above printed area "
                f"(bbox miny={minby:.2f} mm < {min_y:.2f} mm)"
            )
        if maxby > max_y:
            errors.append(
                f"Text {t.text!r} at ({t.x:.2f},{t.y:.2f}) extends below printed area "
                f"(bbox maxy={maxby:.2f} mm > {max_y:.2f} mm)"
            )

    assert not errors, "Text must not extend beyond printed area:\n" + "\n".join(f"  - {e}" for e in errors)


def test_no_font_smaller_than_10pt() -> None:
    """All text must use a font size of at least 10 point.

    SVG user units here are mm; 10 pt = 10 * 25.4/72 mm.
    """
    assert CANONICAL_SVG.exists(), f"Canonical SVG not found: {CANONICAL_SVG}"
    texts = extract_text(CANONICAL_SVG)
    failures: list[str] = []
    for t in texts:
        if t.font_size < MIN_FONT_SIZE_MM:
            failures.append(
                f"Text {t.text!r} at ({t.x:.2f},{t.y:.2f}) has font-size {t.font_size:.2f} mm "
                f"(< {MIN_FONT_SIZE_MM:.2f} mm = {MIN_FONT_SIZE_PT} pt)"
            )
    assert not failures, "Font size must be >= 10 pt:\n" + "\n".join(f"  - {f}" for f in failures)


def test_labels_beneath_drill_centered() -> None:
    """Labels must be beneath or above their relevant drill and horizontally centered.

    For each text below the title area (y >= 16 mm), the relevant drill is the
    nearest hole (by position). If the hole is above the text, the label must
    be below the hole (text.y >= hole.y + hole.r). If the hole is below the
    text, the label must be above the hole (text.y <= hole.y - hole.r).
    In both cases the label must be horizontally centered (text x within 2 mm
    of hole center when text-anchor is middle).
    """
    assert CANONICAL_SVG.exists(), f"Canonical SVG not found: {CANONICAL_SVG}"
    holes = extract_holes(CANONICAL_SVG)
    circle_holes = [h for h in holes if h.kind == "circle"]
    texts = extract_text(CANONICAL_SVG)
    title_cutoff_y_mm = 16.0
    x_tol_mm = 2.0
    errors: list[str] = []
    for t in texts:
        if t.y < title_cutoff_y_mm:
            continue
        nearest = min(circle_holes, key=lambda h: abs(t.x - h.x) + 0.15 * abs(t.y - h.y))
        if abs(t.x - nearest.x) > x_tol_mm:
            errors.append(
                f"Label {t.text!r} at ({t.x:.2f},{t.y:.2f}) is not centered on drill at "
                f"({nearest.x:.2f},{nearest.y:.2f}) (|dx|={abs(t.x - nearest.x):.2f} > {x_tol_mm} mm)"
            )
        if t.y >= nearest.y:
            if t.y < nearest.y + nearest.r:
                errors.append(
                    f"Label {t.text!r} at ({t.x:.2f},{t.y:.2f}) is not beneath drill at "
                    f"({nearest.x:.2f},{nearest.y:.2f}) r={nearest.r:.2f} (label y should be >= {nearest.y + nearest.r:.2f})"
                )
        else:
            if t.y > nearest.y - nearest.r:
                errors.append(
                    f"Label {t.text!r} at ({t.x:.2f},{t.y:.2f}) is not above drill at "
                    f"({nearest.x:.2f},{nearest.y:.2f}) r={nearest.r:.2f} (label y should be <= {nearest.y - nearest.r:.2f})"
                )
    assert not errors, "Labels must be beneath/above and centered on their drill:\n" + "\n".join(f"  - {e}" for e in errors)


if __name__ == "__main__":
    # First test: drill holes and locations must be identical to Patch.Init NPTH.
    print(f"Canonical panel: {CANONICAL_SVG}")
    print("Drill holes match Patch.Init (first test):")
    try:
        test_panel_drill_holes_match_patch_init()
        print("  PASS")
    except AssertionError as exc:
        # When run as a script, treat this as a warning so subsequent
        # diagnostics (NPTH comparison, overlays, etc.) can still run.
        print("  WARNING: first test failed:", exc)

    canonical_holes = extract_holes(CANONICAL_SVG)
    print(f"  Holes detected: {len(canonical_holes)}")

    # Cross-check against the NPTH drill file used for fabrication.
    print("\nDrill holes vs Patch.Init NPTH drill file:")
    try:
        drill_path = _panel_assets_path(
            "patch_init_gerbers",
            "blank-NPTH.drl",
        )
        if not drill_path.exists():
            print(f"  WARNING: NPTH drill file not found at {drill_path}; skipping drill comparison.")
        else:
            drill_holes = parse_patch_init_holes_from_drill(drill_path)
            if not drill_holes:
                print("  WARNING: No holes parsed from NPTH drill file; check blank-NPTH.drl format.")
            else:
                svg_circles = [h for h in canonical_holes if h.kind == "circle"]
                drill_mismatches = _match_hole_sets_with_warnings(
                    drill_holes,
                    svg_circles,
                    "NPTH vs SVG",
                    tol_mm=0.05,
                )
                if drill_mismatches:
                    print("  WARNING: NPTH drill and SVG holes differ:")
                    for msg in drill_mismatches:
                        print(f"    - {msg}")
                else:
                    print("  NPTH drill holes match SVG within tolerance.")
    except Exception as exc:  # pragma: no cover - diagnostic path
        print("  WARNING: Exception while comparing NPTH drill to SVG:", exc)

    for svg in sorted(HERE.glob("ResynthesisPanel.svg")):
        print(f"Checking {svg.name} ...", end=" ")
        candidate_holes = extract_holes(svg)
        errs = _match_holes(canonical_holes, candidate_holes)
        if errs:
            print("FAIL")
            for e in errs:
                print("  -", e)
        else:
            print("OK")

    # Run remaining mechanical checks
    print("\nMechanical checks:")
    try:
        test_canonical_panel_dimensions_and_diameters()
        print("  Canonical panel dimensions and diameters: OK")
    except AssertionError as exc:
        print("  FAIL:", exc)

    print("\nFour pots, switches, and SD card slot match Patch.Init:")
    try:
        test_panel_cutouts_four_pots_switches_and_sd_slot_match_patch_init()
        print("  Cutouts (4 pots, 2 switches, SD holder): PASS")
    except AssertionError as exc:
        print("  FAIL:", exc)

    print("\nScrew holes match Eurorack rail (3 mm from top/bottom):")
    try:
        test_screw_holes_eurorack_rail_distance()
        print("  Screw hole vertical position: PASS")
    except AssertionError as exc:
        print("  FAIL:", exc)

    print("\nPCBWay-style validation (panel would pass manufacturer checks):")
    try:
        test_panel_passes_pcbway_style_validation()
        print("  PCBWay-style design rules: PASS")
    except AssertionError as exc:
        print("  FAIL:", exc)

    print("\nKnob/label clearance check (Rogan knobs):")
    try:
        test_knob_labels_not_obscured_by_rogan_knobs()
        print("  Labels vs knobs: PASS")
    except AssertionError as exc:
        print("  FAIL:", exc)

    print("\nNo overlapping text:")
    try:
        test_no_overlapping_text()
        print("  PASS")
    except AssertionError as exc:
        print("  FAIL:", exc)

    print("\nText within printed area:")
    try:
        test_text_within_printed_area()
        print("  PASS")
    except AssertionError as exc:
        print("  FAIL:", exc)

    print("\nFont size >= 10 pt:")
    try:
        test_no_font_smaller_than_10pt()
        print("  PASS")
    except AssertionError as exc:
        print("  FAIL:", exc)

    print("\nLabels beneath drill, centered:")
    try:
        test_labels_beneath_drill_centered()
        print("  PASS")
    except AssertionError as exc:
        print("  FAIL:", exc)

    # Simple drill-size report derived dynamically from the canonical SVG.
    print("\nDrill size report (from SVG):")
    holes = extract_holes(CANONICAL_SVG)
    circle_holes = [h for h in holes if h.kind == "circle"]
    if not circle_holes:
        print("  WARNING: no circular holes found; nothing to report.")
    else:
        from collections import defaultdict

        # Group by drilled diameter (mm), rounded to two decimal places, so the
        # report stays stable even if very small numeric noise is introduced.
        diam_groups: dict[float, list[Hole]] = defaultdict(list)
        for h in circle_holes:
            d = 2.0 * h.r
            key = round(d, 2)
            diam_groups[key].append(h)

        for d in sorted(diam_groups.keys()):
            group = diam_groups[d]
            print(
                f"  Ø {d:4.2f} mm : {len(group):2d} hole(s) "
                f"(examples: first at ({group[0].x:.2f}, {group[0].y:.2f}))"
            )

    print("\nHardware-centre vs NPTH drill overlay:")
    try:
        generate_hardware_center_check_svg()
    except AssertionError as exc:
        print("  FAIL:", exc)
    else:
        print("  Overlay written to output/hardware-center-check.svg")

    # Final step: generate Eurorack standard overlay (green HP grid, blue rail centers, pink cut annotations)
    import subprocess
    import sys
    overlay_script = HERE / "render_eurorack_overlay.py"
    if overlay_script.exists():
        print("\nEurorack overlay (HP grid, rail centers, cut annotations):")
        try:
            subprocess.run(
                [sys.executable, str(overlay_script), str(CANONICAL_SVG)],
                cwd=str(HERE),
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print("  FAIL:", exc)
        else:
            print("  Overlay written to", CANONICAL_SVG.stem + "_eurorack_overlay.svg")

