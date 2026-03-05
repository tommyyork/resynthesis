# Resynthesis Panel – Component Datasheets

This folder contains datasheets for **non-passive** components used on the Resynthesis panel (KiCAD project: `panel/assets/KiCad_PCB/ES_Daisy_Patch_SM_FB_Rev1`). Resistors, capacitors, and diodes are excluded.

## Component → Part Number → Datasheet

| KiCAD ref / value       | Part number / description        | Datasheet file | Panel cutout (recommended) |
|-------------------------|-----------------------------------|----------------|-----------------------------|
| **U1**                  | ES_DAISY_PATCH_SM_REV1 (Daisy Patch SM) | `ES_Patch_SM_datasheet_v1.0.5.pdf` | — |
| **SW_1**                 | TL1105SPF250Q (tactile switch)    | `E-Switch_TL1105_series.pdf` | 5.5 mm ⌀ panel (5.5 mm drill + 0.0 mm clearance) |
| **SW_2**                 | TOGGLE_ON-ON (e.g. C&K TS series) | `C-K_TS_series_toggle.pdf` | 6.3 mm ⌀ panel (6.2 mm drill + 0.1 mm clearance) |
| **VR_1–VR_4**           | M-10K-B-D (9 mm snap-in 10K pot)  | `Bourns_PTV09.pdf` (also compatible with Alpha 9 mm style) | 7.5 mm ⌀ panel (7.2 mm drill + 0.3 mm clearance) |
| **J_* (EURO_JACK)**     | 3.5 mm mono jack (e.g. PJ-301BM / Thonkiconn) | `PJ-301BM_3.5mm_jack.pdf` | 6.3 mm ⌀ panel (6.2 mm drill + 0.1 mm clearance) |
| **U_SDCARD1**            | MICRO_SD_CARDCENTERED (e.g. PJS008U-3000-0) | `Yamaichi_PJS008U-3000-0_microSD.pdf` | — |
| **EP1**                  | EURORACK_POWERLOCK                | — (generic 2×5 2.54 mm shrouded header; see Daisy Patch SM datasheet for power) | — |
| **P1**                   | M12 (1×12 pin header)             | — (generic 2.54 mm pin header) | — |
| **LED_1**                | LED                               | — (diode; excluded per request) | 3.2 mm ⌀ panel (3.2 mm drill + 0.0 mm clearance) |

## Notes

- **Panel cutouts:** The recommended panel cutout diameters in the table are taken from the Resynthesis panel design: `panel/generate_resynthesis_panel_svg.py` and the Patch.Init NPTH drill file `panel/assets/patch_init_gerbers/blank-NPTH.drl`. They match the circular holes in the generated panel SVG and the footprint overlays in `panel/footprint_calc/*_panel_overlay.svg`.
- **Potentiometers:** The schematic value `M-10K-B-D` matches 9 mm snap-in, 10K linear, D-shaft pots. The Bourns PTV09 datasheet is included as a representative 9 mm pot; the Daisy Patch SM datasheet also references Alpha 9 mm linear 10K (e.g. RD901F-40-15F-B10K-00D70).
- **3.5 mm jacks:** The footprint is `S_JACK`; the Daisy Patch SM datasheet recommends Thonkiconn (e.g. WQP-WQP518MA). The PJ-301BM datasheet is included as a common equivalent.
- **Toggle:** The schematic shows TOGGLE_ON-ON; the C&K TS series datasheet is included as a typical subminiature toggle. The Daisy datasheet also references 2MS1T1B1M2QES and TS-4A-TECQ-H.
- **Micro SD:** The Daisy Patch SM recommends vertical MicroSD connector PJS008U-3000-0 (Yamaichi); the KiCAD value is MICRO_SD_CARDCENTERED with footprint VERT_MICROSD_CENTERED.

## Source

Datasheets were obtained from manufacturer or distributor sites (E-Switch, Bourns, C&K, Yamaichi, Thonk, Electrosmith CDN) for use with the Resynthesis panel design.
