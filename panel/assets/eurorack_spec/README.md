# Eurorack mechanical reference

This folder holds references to the **Eurorack / Doepfer A-100** mechanical standard used to validate panel mounting holes (screw positions) and dimensions.

## Official sources

- **Doepfer A-100 Construction Details**  
  [https://doepfer.de/a100_man/a100m_e.htm](https://doepfer.de/a100_man/a100m_e.htm)  
  Describes panel height (128.5 mm for 3U), width in HP (1 HP = 5.08 mm), mounting hole layout, M3×6 screws, and front panel width table.

- **Doepfer A-100 frame construction (PDF)**  
  [https://doepfer.de/a100_man/A100G6_e.pdf](https://doepfer.de/a100_man/A100G6_e.pdf)  
  Internal technical document; includes dimensional sketches. For qualified personnel only (mains voltage).

- **Exploding Shed – Eurorack dimensions**  
  [https://www.exploding-shed.com/synth-diy-guides/standards-of-eurorack/eurorack-dimensions/](https://www.exploding-shed.com/synth-diy-guides/standards-of-eurorack/eurorack-dimensions/)  
  Summary of 3U panel height, HP width, and mounting hole spacing.

- **Gie-Tec rail profiles (PDF)**  
  [https://www.gie-tec.de/downloads/db_zubehoer19-zoll.pdf](https://www.gie-tec.de/downloads/db_zubehoer19-zoll.pdf)  
  Rail manufacturer; rail mounting hole spacing matches front-panel hole spacing (122.5 mm between rows).

## Key dimensions (3U panels)

| Quantity | Value | Notes |
|----------|--------|------|
| Panel height | 128.5 mm | 3U; reduced from 133.4 mm to account for rail lip |
| Panel width (10 HP) | 50.8 mm (calc) / 50.5 mm (actual) | Slightly under N×5.08 mm for assembly tolerance |
| 1 HP | 5.08 mm | Horizontal pitch |
| Distance between mounting hole rows | 122.5 mm | Doepfer mechanical page; center-to-center vertical |
| **Mounting hole center from top edge** | **3 mm** | So top row center at y = 3 mm in panel coordinates |
| **Mounting hole center from bottom edge** | **3 mm** | So bottom row center at y = 128.5 − 3 = 125.5 mm |
| Horizontal hole position (from left/right edge) | 7.5 mm | Typical; hole centers at 7.5 mm and (width − 7.5) mm |
| Screw | M3×6 oval head (DIN7985) | 3.2 mm hole; oval slots common for tolerance |

The **3 mm** distance from top/bottom edge to hole center is derived from: panel height 128.5 mm, distance between hole rows 122.5 mm ⇒ 128.5 − 122.5 = 6 mm total "margin," i.e. 3 mm from each edge to the nearest hole center. This matches common practice and Doepfer/Gie-Tec compatibility.

## Use in this project

The panel test `test_screw_holes_eurorack_rail_distance` in `test_panel_alignment.py` verifies that the four corner screw cutouts in `ResynthesisPanel.svg` have their **vertical** centers at 3 mm from the top edge and 3 mm from the bottom edge (same as the rail), within a small tolerance (0.5 mm). Horizontal positions are validated separately against the Patch.Init drill file.
