#!/usr/bin/env python3
"""
Generate an Eurorack standard overlay for a panel SVG.

Reads a panel SVG, extracts its dimensions and all cut/hole centers, then
outputs an overlay SVG with:
  - Green dotted vertical lines at 2HP, 4HP, 6HP, 8HP boundaries (1 HP = 5.08 mm).
  - Neon blue dotted horizontal lines at the vertical center of the Eurorack
    rails when the module is mounted (3 mm from top and bottom per Doepfer A-100).
  - Neon pink annotations: (x, y) at the center of each cut/hole, plus panel
    width and height.

The overlay uses the same viewBox as the panel so it can be opened in Inkscape
(or another viewer) on top of the panel for alignment reference. Dimensions
follow panel/eurorack_spec/README.md.

Usage:
  python3 render_eurorack_overlay.py [panel.svg]
  python3 render_eurorack_overlay.py panel.svg -o overlay.svg

Default input: ResynthesisPanel.svg (in script directory).
Default output: <stem>_eurorack_overlay.svg next to the input file.
"""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


HERE = Path(__file__).parent

# Patch.Init panel origin (from blank-Edge_Cuts.gbr): board left/top in Gerber mm.
# These constants match the values used in generate_resynthesis_panel_svg.py so
# that NPTH drills and Edge_Cuts geometry can be mapped into the same
# panel-local coordinate system as the SVG artwork.
PATCH_INIT_PANEL_ORIGIN_X_MM = 26.545
PATCH_INIT_PANEL_ORIGIN_Y_MM = -27.095  # Gerber/Excellon Y is negative downward
OY_TOP_MM = 27.095  # -Gerber Y for the top edge

# Eurorack 3U (Doepfer A-100 / eurorack_spec/README.md)
HP_MM = 5.08
PANEL_HEIGHT_MM = 128.5
RAIL_CENTER_FROM_TOP_MM = 3.0
RAIL_CENTER_FROM_BOTTOM_MM = 3.0  # => bottom rail center at PANEL_HEIGHT_MM - 3

# Overlay style (scaled up for visibility when combined with panel)
GREEN_HP_LINES = "#00cc66"
BLUE_RAIL_LINES = "#00d4ff"
PINK_ANNOTATIONS = "#ff00ff"  # neon pink for dimension annotations
STROKE_WIDTH = 0.3
DASH_ARRAY = "1.2 0.8"
# 3.0 mm ≈ 8.5 pt; make dimension text ~2 pt smaller.
ANNOTATION_FONT_SIZE = 2.3
OVERLAY_FONT_FAMILY = "Gidole, 'DIN Alternate', 'DIN 2014', sans-serif"

# Smaller font for original hardware names shown beneath panel labels.
# 2.4 mm ≈ 6.8 pt; make sublabels ~1 pt larger.
ORIGINAL_NAME_FONT_SIZE = 2.75
ORIGINAL_NAME_COLOR = "#89cff0"  # baby blue for original labels
# Vertical clearance (mm) between panel label baseline and original-name overlay.
ORIGINAL_NAME_CLEARANCE_MM = 1.0

# Original hardware names / IDs for the Patch.Init layout used by the
# Resynthesis firmware (panel‑local mm coordinates). Coordinates are at the
# hardware centers (pots, jacks, switches, LED), derived from the Patch.Init
# Gerber drill and edge files used elsewhere in this folder.
#
# For easier cross‑referencing with the stock Patch.Init documentation and
# KiCad/EAGLE design files, each entry also includes the **Daisy Patch SM
# header silkscreen label** (B‑/C‑row pin) taken from the top silkscreen
# layer (`top_silk.GTO`). These are shown as a second line using \"\\n\" so
# the overlay renders e.g. `CV_1` over `C5` at the corresponding hardware
# center.
#
# Header mappings (from the official schematic / silkscreen):
#   - Pots: CV_1 → C5, CV_2 → C4, CV_3 → C3, CV_4 → C2.
#   - CV inputs / gate I/O: CV_5 → B10, CV_6 → B11, CV_7 → B5, CV_8 → B6.
#   - Audio I/O: IN_L → B4, IN_R → B3, OUT_L → B2, OUT_R → B1.
#   - CV outputs: CV_OUT_1 → C10, CV_OUT_2 → C1.
# Some labels (e.g. CV_OUT_1 with C10) are rendered as two lines via \"\\n\".
ORIGINAL_NAME_LABELS: list[tuple[str, float, float]] = [
    # Pots CV_1–CV_4 (with Patch SM header pins C5–C2 and KiCad VR_ refs)
    ("CV_1\nVR_1", 11.176, 22.904),
    ("CV_2\nVR_2", 39.65, 22.904),
    ("CV_3\nVR_3", 11.176, 42.027),
    ("CV_4\nVR_4", 39.65, 42.027),
    # Switches: B_7 (reserved), B_8 (pitch lock), and the CV_OUT_1 jack (THOUGHTS).
    # Coordinates are taken from the Patch.Init drill/edge Gerbers and remain
    # fixed even if the panel artwork or naming conventions evolve.
    ("B7\nSW_1", 8.65, 59.288),
    ("B8\nSW_2", 25.4, 59.288),
    ("C10\nJ_CVOUT1", 42.155, 59.288),
    # Top jack row (gates on Patch SM header pins B10, B11, B5, B6)
    ("B10\nJ_GATEIN1", 7.15, 84.562),
    ("B9\nJ_GATEIN2", 19.317, 84.562),
    ("B5\nJ_GATEOUT1", 31.483, 84.562),
    ("B6\nJ_GATEOUT2", 43.65, 84.562),
    # Middle jack row (CV inputs CV_5–CV_8 on J_CV1–J_CV4)
    ("CV_5\nJ_CV1", 7.15, 98.312),
    ("CV_6\nJ_CV2", 19.317, 98.312),
    ("CV_7\nJ_CV3", 31.483, 98.312),
    ("CV_8\nJ_CV4", 43.65, 98.312),
    # Bottom jack row (audio I/O; Patch SM header pins B4–B1)
    ("B4\nJ_LIN1", 7.15, 111.9),
    ("B3\nJ_RIN1", 19.317, 111.9),
    ("B2\nJ_LOUT1", 31.483, 111.9),
    ("B1\nJ_ROUT1", 43.65, 111.9),
    # LED above title area
    ("LED_1", 25.4, 19.252),
]


def _parse_float(value: str | None) -> float:
    if value is None:
        raise ValueError("Missing numeric attribute")
    return float(eval(value, {}, {}))


def get_svg_viewbox(svg_path: Path) -> tuple[float, float, float, float]:
    """Return (min_x, min_y, width, height) from SVG root viewBox or width/height."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    vb = root.get("viewBox")
    if vb:
        parts = vb.strip().split()
        if len(parts) == 4:
            return tuple(float(x) for x in parts)
    w = root.get("width", "0")
    h = root.get("height", "0")
    w = float(re.sub(r"[^0-9.]", "", w) or "0")
    h = float(re.sub(r"[^0-9.]", "", h) or "0")
    return (0.0, 0.0, w, h)


@dataclass(frozen=True)
class Cut:
    kind: str          # "circle" or "rect"
    cx: float
    cy: float
    r: float | None    # radius for circles (mm)
    w: float | None    # width for rects (mm)
    h: float | None    # height for rects (mm)
    rx: float | None   # corner radius for rects (mm)


def _panel_assets_drill_path() -> Path:
    """Return the path to the Patch.Init NPTH drill file in the panel assets."""
    return HERE / "assets" / "patch_init_gerbers" / "blank-NPTH.drl"


def _panel_assets_edge_cuts_path() -> Path:
    """Return the path to the Patch.Init Edge_Cuts file in the panel assets."""
    return HERE / "assets" / "patch_init_gerbers" / "blank-Edge_Cuts.gbr"


def _gerber_x46_to_mm(val: int) -> float:
    """Convert Gerber 4.6 format (4 int, 6 decimal) to mm."""
    return val / 1e6


def _load_cuts_from_drill_and_edge_cuts() -> list[Cut]:
    """Load panel cuts from Patch.Init NPTH drill and Edge_Cuts Gerbers.

    This uses the same panel-local coordinate system as the alignment tests and
    panel generator: origin at the top-left of the panel, X to the right, Y
    down. Circular NPTH drills become `Cut(kind="circle")` entries and the SD
    card slot from Edge_Cuts becomes a rectangular `Cut(kind="rect")`.
    """
    cuts: list[Cut] = []

    # 1) Circular NPTH drills from blank-NPTH.drl
    drill_path = _panel_assets_drill_path()
    if drill_path.exists():
        text = drill_path.read_text(encoding="utf-8", errors="ignore")

        import re as re_mod

        tool_diam_mm: dict[str, float] = {}
        current_tool: str | None = None

        # Tool definitions: T1C3.000, T2C3.200, ...
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            m_tool = re_mod.match(r"^T(\d+)C([0-9.]+)", line)
            if m_tool:
                tool_id = f"T{m_tool.group(1)}"
                tool_diam_mm[tool_id] = float(m_tool.group(2))
                continue

        # Second pass for coordinates.
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue

            m_select = re_mod.match(r"^T(\d+)\s*$", line)
            if m_select:
                current_tool = f"T{m_select.group(1)}"
                continue

            if "X" not in line or "Y" not in line:
                continue

            coords = re_mod.findall(r"X([-\d.]+)Y([-\d.]+)", line)
            if not coords or current_tool is None:
                continue
            diam = tool_diam_mm.get(current_tool)
            if diam is None:
                continue
            r = diam / 2.0

            for xs, ys in coords:
                gx = float(xs)
                gy = float(ys)
                lx = gx - PATCH_INIT_PANEL_ORIGIN_X_MM
                ly = -gy - OY_TOP_MM
                cuts.append(
                    Cut(
                        kind="circle",
                        cx=lx,
                        cy=ly,
                        r=r,
                        w=None,
                        h=None,
                        rx=None,
                    )
                )

    # 2) SD card slot from blank-Edge_Cuts.gbr (rectangular cutout).
    edge_path = _panel_assets_edge_cuts_path()
    if edge_path.exists():
        text = edge_path.read_text(encoding="utf-8", errors="ignore")

        import re as re_mod

        ox = PATCH_INIT_PANEL_ORIGIN_X_MM
        oy_top = OY_TOP_MM

        points: list[tuple[float, float]] = []
        for match in re_mod.finditer(r"X(-?\d+)Y(-?\d+)", text, re_mod.IGNORECASE):
            gx_i = int(match.group(1))
            gy_i = int(match.group(2))
            gx = _gerber_x46_to_mm(gx_i)
            gy = _gerber_x46_to_mm(gy_i)
            lx = gx - ox
            ly = -gy - oy_top
            points.append((lx, ly))

        # Find all axis-aligned rectangles (consecutive 4 points that form a bbox).
        rects: list[tuple[float, float, float, float]] = []
        for i in range(len(points) - 3):
            xs = [points[i + j][0] for j in range(4)]
            ys = [points[i + j][1] for j in range(4)]
            xmin, xmax = min(xs), max(xs)
            ymin, ymax = min(ys), max(ys)
            w = xmax - xmin
            h = ymax - ymin
            if w >= 2.0 and h >= 2.0:
                rects.append((xmin, ymin, w, h))

        # The SD slot is the rectangle that is not the board outline (50.8 x 128.5).
        for (x, y, w, h) in rects:
            if 2.0 <= w <= 5.0 and 8.0 <= h <= 18.0:
                cuts.append(
                    Cut(
                        kind="rect",
                        cx=x + w / 2.0,
                        cy=y + h / 2.0,
                        r=None,
                        w=w,
                        h=h,
                        rx=0.0,
                    )
                )
                break

    return cuts


def extract_cuts(svg_path: Path) -> list[Cut]:
    """Extract all panel cuts/holes (circles and black-filled rects) with size info."""
    tree = ET.parse(svg_path)
    root = tree.getroot()
    parent_map = {c: p for p in root.iter() for c in p}

    def inside_defs(el: ET.Element) -> bool:
        tag = (el.tag.split("}")[-1] if "}" in el.tag else el.tag).lower()
        if tag in ("defs", "pattern", "mask", "lineargradient", "radialgradient"):
            return True
        p = parent_map.get(el)
        return p is not None and inside_defs(p)

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    cuts: list[Cut] = []

    for el in root.iter(f"{ns}circle"):
        if inside_defs(el):
            continue
        cx = el.get("cx")
        cy = el.get("cy")
        r = el.get("r")
        if cx is None or cy is None or r is None:
            continue
        r_val = _parse_float(r)
        if r_val < 0.5:
            continue
        cuts.append(
            Cut(
                kind="circle",
                cx=_parse_float(cx),
                cy=_parse_float(cy),
                r=r_val,
                w=None,
                h=None,
                rx=None,
            )
        )

    for el in root.iter(f"{ns}rect"):
        if inside_defs(el):
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
        x = _parse_float(x_attr)
        y = _parse_float(y_attr)
        w = _parse_float(w_attr)
        h = _parse_float(h_attr)
        if w < 0.5 and h < 0.5:
            continue
        rx_attr = el.get("rx") or el.get("ry")
        rx_val = _parse_float(rx_attr) if rx_attr is not None else 0.0
        cuts.append(
            Cut(
                kind="rect",
                cx=x + w / 2.0,
                cy=y + h / 2.0,
                r=None,
                w=w,
                h=h,
                rx=rx_val,
            )
        )

    return cuts


def _extract_panel_text_labels(panel_svg: Path) -> list[tuple[float, float, float, str]]:
    """Return (x, y, font_size, text) for visible text in the panel SVG."""
    tree = ET.parse(panel_svg)
    root = tree.getroot()
    parent_map = {c: p for p in root.iter() for c in p}

    def inside_defs(el: ET.Element) -> bool:
        tag = (el.tag.split("}")[-1] if "}" in el.tag else el.tag).lower()
        if tag in ("defs", "pattern", "mask", "lineargradient", "radialgradient"):
            return True
        p = parent_map.get(el)
        return p is not None and inside_defs(p)

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    labels: list[tuple[float, float, float, str]] = []
    for el in root.iter(f"{ns}text"):
        if inside_defs(el):
            continue
        x_attr = el.get("x")
        y_attr = el.get("y")
        fs_attr = el.get("font-size")
        if x_attr is None or y_attr is None or fs_attr is None:
            continue
        text = "".join(el.itertext()).strip()
        if not text:
            continue
        labels.append(
            (
                _parse_float(x_attr),
                _parse_float(y_attr),
                _parse_float(fs_attr),
                " ".join(text.split()),
            )
        )
    return labels


def build_overlay_svg(
    panel_width_mm: float,
    panel_height_mm: float,
    cuts: list[Cut],
    panel_href: str,
    hp_count: int | None = None,
) -> str:
    """Build overlay SVG as a string, embedding the panel underneath the overlay."""
    if hp_count is None:
        hp_count = max(1, int(round(panel_width_mm / HP_MM)))

    rail_top_y = RAIL_CENTER_FROM_TOP_MM
    rail_bottom_y = panel_height_mm - RAIL_CENTER_FROM_BOTTOM_MM

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        "<!-- Eurorack standard overlay: HP grid (green), rail centers (blue), cut annotations (pink). "
        "Panel artwork is embedded underneath. See eurorack_spec/README.md. -->",
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {panel_width_mm} {panel_height_mm}" '
        f'width="{panel_width_mm}mm" height="{panel_height_mm}mm">',
        # Embed the original panel SVG as an <image> so the overlay renders on top.
        f'  <image href="{panel_href}" x="0" y="0" width="{panel_width_mm}" height="{panel_height_mm}" />',
        '  <g id="eurorack-overlay" opacity="0.7">',
        "    <!-- Green dotted: 2HP vertical grid (1 HP = 5.08 mm) -->",
    ]

    for i in range(0, hp_count + 1, 2):
        x = i * HP_MM
        if x > panel_width_mm + 0.01:
            break
        lines.append(
            f'    <line x1="{x}" y1="0" x2="{x}" y2="{panel_height_mm}" '
            f'stroke="{GREEN_HP_LINES}" stroke-width="{STROKE_WIDTH}" '
            f'stroke-dasharray="{DASH_ARRAY}" />'
        )

    lines.extend([
        "    <!-- Neon blue dotted: vertical center of rails when module is mounted -->",
        f'    <line x1="0" y1="{rail_top_y}" x2="{panel_width_mm}" y2="{rail_top_y}" '
        f'stroke="{BLUE_RAIL_LINES}" stroke-width="{STROKE_WIDTH}" '
        f'stroke-dasharray="{DASH_ARRAY}" />',
        f'    <line x1="0" y1="{rail_bottom_y}" x2="{panel_width_mm}" y2="{rail_bottom_y}" '
        f'stroke="{BLUE_RAIL_LINES}" stroke-width="{STROKE_WIDTH}" '
        f'stroke-dasharray="{DASH_ARRAY}" />',
        "    <!-- Neon pink: size at each cut center + panel dimensions -->",
    ])

    for cut in sorted(cuts, key=lambda c: (c.cy, c.cx)):
        if cut.kind == "circle" and cut.r is not None:
            d = 2.0 * cut.r
            label = f"{d:.1f}mm ⌀"
        elif cut.kind == "rect" and cut.w is not None and cut.h is not None:
            # Rectangular cut: width × height, optional corner radius.
            label = f"{cut.w:.1f}×{cut.h:.1f}mm"
            if cut.rx is not None and cut.rx > 0.0:
                label += f", {cut.rx:.1f}mm r"
        else:
            label = ""
        if not label:
            continue
        lines.append(
            f'    <text x="{cut.cx:.3f}" y="{cut.cy:.3f}" font-size="{ANNOTATION_FONT_SIZE}" '
            f'fill="{PINK_ANNOTATIONS}" text-anchor="middle" dominant-baseline="middle" '
            f'font-family="{OVERLAY_FONT_FAMILY}">'
            f"{label}</text>"
        )

    # For the Resynthesis panel (3U x 10HP), also show the original hardware
    # names for pots, jacks, LED, and switches in a smaller font and distinct
    # colour. The Y-position of each original name is automatically moved down
    # far enough to clear the corresponding panel label text, without changing
    # any other layout.
    if abs(panel_width_mm - 50.8) < 0.5 and abs(panel_height_mm - 128.5) < 0.5:
        panel_svg_path = Path(__file__).parent / panel_href
        panel_labels = _extract_panel_text_labels(panel_svg_path) if panel_svg_path.exists() else []
        lines.append("    <!-- Original hardware names beneath panel labels -->")

        for name, cx, cy in ORIGINAL_NAME_LABELS:
            # Default position: just below the hardware center.
            target_y = cy + 2.0 * ORIGINAL_NAME_FONT_SIZE

            # If we have panel labels, find the nearest label by X that is
            # associated with this hardware and place the original name just
            # beneath it.
            # Special-case: B8 overlay should stay level with B7 and C10
            # regardless of how the panel artwork labels are arranged, so we
            # skip auto-snapping to panel text for that entry.
            if panel_labels and name != "LED_1" and not name.startswith("B8\n"):
                # Prefer labels that are roughly aligned in X and below the hole.
                candidates: list[tuple[float, float, float, str]] = []
                for lx, ly, lfs, ltext in panel_labels:
                    if abs(lx - cx) <= 2.0 and ly >= cy:
                        candidates.append((lx, ly, lfs, ltext))
                if not candidates:
                    # Fallback: any label roughly aligned in X.
                    for lx, ly, lfs, ltext in panel_labels:
                        if abs(lx - cx) <= 2.0:
                            candidates.append((lx, ly, lfs, ltext))
                if candidates:
                    _, ly, lfs, _ = min(
                        candidates,
                        key=lambda item: (abs(item[0] - cx), item[1]),
                    )
                    target_y = ly + lfs + ORIGINAL_NAME_CLEARANCE_MM

            # Support simple two-line labels using '\n' (e.g. "CV_OUT_1\nC10").
            parts = name.split("\n")
            if len(parts) == 1:
                lines.append(
                    f'    <text x="{cx:.3f}" y="{target_y:.3f}" font-size="{ORIGINAL_NAME_FONT_SIZE}" '
                    f'fill="{ORIGINAL_NAME_COLOR}" text-anchor="middle" '
                    f'font-family="{OVERLAY_FONT_FAMILY}">{name}</text>'
                )
            else:
                # First line at target_y, subsequent lines spaced by font size.
                for i, part in enumerate(parts):
                    y_line = target_y + i * ORIGINAL_NAME_FONT_SIZE
                    lines.append(
                        f'    <text x="{cx:.3f}" y="{y_line:.3f}" font-size="{ORIGINAL_NAME_FONT_SIZE}" '
                        f'fill="{ORIGINAL_NAME_COLOR}" text-anchor="middle" '
                        f'font-family="{OVERLAY_FONT_FAMILY}">{part}</text>'
                    )

    lines.append(
        f'    <text x="{panel_width_mm / 2}" y="{panel_height_mm - 1}" '
        f'font-size="{ANNOTATION_FONT_SIZE}" fill="{PINK_ANNOTATIONS}" text-anchor="middle" '
        f'font-family="{OVERLAY_FONT_FAMILY}">'
        f"panel {panel_width_mm:.1f} × {panel_height_mm:.1f} mm</text>"
    )
    lines.append("  </g>")
    lines.append("</svg>")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Eurorack standard overlay SVG for a panel."
    )
    parser.add_argument(
        "panel_svg",
        nargs="?",
        type=Path,
        default=HERE / "output" / "ResynthesisPanel.svg",
        help="Input panel SVG (default: output/ResynthesisPanel.svg in script dir)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output overlay SVG (default: <stem>_eurorack_overlay.svg)",
    )
    parser.add_argument(
        "--hp",
        type=int,
        default=None,
        help="Panel width in HP for grid (default: from panel width / 5.08)",
    )
    args = parser.parse_args()

    panel_path = args.panel_svg.resolve()
    if not panel_path.exists():
        raise SystemExit(f"Panel SVG not found: {panel_path}")

    _, _, w, h = get_svg_viewbox(panel_path)
    if w <= 0 or h <= 0:
        raise SystemExit(f"Invalid panel dimensions: {w} x {h}")

    # Prefer canonical manufacturing data from the Patch.Init Gerbers when
    # available so that cut centers and diameters come directly from the
    # NPTH drills and Edge_Cuts. Fall back to extracting cuts from the panel
    # SVG only if the Gerber assets are missing.
    cuts = _load_cuts_from_drill_and_edge_cuts()
    if not cuts:
        cuts = extract_cuts(panel_path)
    hp_count = args.hp
    if hp_count is None:
        hp_count = max(1, int(round(w / HP_MM)))

    # Use the panel file name as the href so the overlay SVG can find it
    # via a relative path in tools like Inkscape.
    svg = build_overlay_svg(w, h, cuts, panel_href=panel_path.name, hp_count=hp_count)

    out = args.output
    if out is None:
        out = panel_path.parent / f"{panel_path.stem}_eurorack_overlay.svg"
    out = out.resolve()
    out.write_text(svg, encoding="utf-8")
    print(f"Wrote {len(cuts)} cuts, {hp_count} HP → {out}")

    # Optional: also render a PNG with the panel and overlay baked together so
    # tools that do not render <image> backgrounds (e.g. some IDE SVG viewers)
    # can still display the combined result.
    try:
        import cairosvg  # type: ignore[import]
    except ImportError:
        return

    png_out = out.with_suffix(".png")
    try:
        # Render at a higher rasterization scale so the embedded panel artwork
        # and the overlay grid/text share a high effective resolution in the
        # exported PNG. A scale of 4.0 keeps the geometry identical while
        # significantly increasing pixel density to avoid a soft, low‑res
        # background.
        cairosvg.svg2png(url=str(out), write_to=str(png_out), scale=4.0)
    except Exception:
        # Keep SVG even if PNG export fails.
        return
    else:
        print(f"Wrote PNG \u2192 {png_out}")


if __name__ == "__main__":
    main()
