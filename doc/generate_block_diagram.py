#!/usr/bin/env python3
"""
Generate a US Letter–sized block diagram of the Resynthesis audio algorithm.

The diagram is meant for musicians and module purchasers:
it shows the high‑level audio path and how the controls (CV_1–CV_8, B_7, B_8)
shape the sound, without requiring any DSP knowledge.

Output (written next to this script):
- Resynthesis_BlockDiagram_USLetter.pdf
- Resynthesis_BlockDiagram_USLetter.svg

Requirements:
- Graphviz command‑line tool `dot` must be installed and available on PATH.
"""

import subprocess
from pathlib import Path


DOT_SOURCE = """
digraph Resynthesis {
    graph [
        rankdir=LR,
        splines=true,
        nodesep=0.7,
        ranksep=1.0,
        fontsize=14,
        labelloc=top,
        labeljust=left,
        label="Resynthesis — Phase‑Vocoder Resynthesis for Daisy Patch SM\\nHigh‑level audio path and controls (US Letter, landscape)",
        // US Letter 11" × 8.5" in landscape; '!' forces exact size for printing.
        size="11,8.5!"
    ];

    node [
        shape=rect,
        style="rounded,filled",
        fillcolor="#f7f7ff",
        color="#4a4a6a",
        fontname="Helvetica,Arial,sans-serif",
        fontsize=11
    ];

    edge [
        color="#444444",
        arrowsize=0.8,
        fontname="Helvetica,Arial,sans-serif",
        fontsize=9
    ];

    // ------------------------------------------------------------------
    // Audio I/O
    // ------------------------------------------------------------------
    inL  [label="IN L"  shape=doublecircle, fillcolor="#e6f2ff"];
    inR  [label="IN R"  shape=doublecircle, fillcolor="#e6f2ff"];
    outL [label="OUT L" shape=doublecircle, fillcolor="#e6f2ff"];
    outR [label="OUT R" shape=doublecircle, fillcolor="#e6f2ff"];

    // ------------------------------------------------------------------
    // Main audio path blocks
    // ------------------------------------------------------------------
    mix_in [label="Input mix &\\npre‑conditioning\\n\\n• Mix IN L/R to mono\\n• Gentle soft clip\\n• Feed analysis buffer"];

    ana_buf [label="Analysis buffer\\n(FFT window)\\n\\n• Rolling history of audio\\n• Each grain reads a full\\n  window of sound"];

    pvoc [label="Phase‑vocoder resynthesis\\n(ResynthEngine)\\n\\n• Analyze spectrum per grain\\n• Track phase & energy over time\\n• V/OCT maps bins around a\\n  musical fundamental and\\n  focuses most energy into\\n  its harmonic families"];

    spectral [label="Spectral shaping\\n\\n• Magnitude smoothing\\n• FLUFF (granular cloud depth)\\n  - extra diffusion\\n  - analysis-window jitter\\n  - micro-pitch jitter\\n  - per-bin mag jitter\\n• Bright / dark tilt\\n• Sparsity (keep only strongest\\n  bins for metallic tones)\\n• Phase diffusion (noisy clouds)"];

    grains [label="Grain playback &\\n time‑stretch\\n\\n• Overlap‑add grains\\n• Jittered launch timing\\n  for “spray” textures\\n• Time‑stretch / density\\n  around 0.25×–4×"];

    comp [label="Output compressor\\n\\n• 2:1, musical leveling\\n• MAX COMP mode for\\n  aggressive leveling\\n• Keeps module loud and\\n  consistent as a sound source"];

    // Audio path connections
    inL  -> mix_in;
    inR  -> mix_in;
    mix_in -> ana_buf;
    ana_buf -> pvoc;
    pvoc -> spectral;
    spectral -> grains;
    grains -> comp;
    comp -> outL;
    comp -> outR;

    // ------------------------------------------------------------------
    // Controls cluster (CVs and switches)
    // ------------------------------------------------------------------
    subgraph cluster_controls {
        label = "Controls (front panel)";
        labelloc = top;
        labeljust = left;
        style = "rounded,dashed";
        color = "#bbbbdd";

        cv1 [label="CV_1 — OFFER / FEED\\nSend amount + dry/wet\\nCCW: dry, unshifted input\\nCW: pitched, granular voice\\nwhen V/OCT is patched" shape=rect fillcolor="#fff7e6"];
        cv2 [label="CV_2 — Time‑stretch / density (TIMESTRETCH)\\nLeft: slower, smeared clouds\\nRight: denser, faster motion\\naround 1× time" shape=rect fillcolor="#fff7e6"];
        cv3 [label="CV_3 — FLUFF\\nGranular cloud depth:\\nadds diffusion, jitter,\\n& micro‑modulation" shape=rect fillcolor="#fff7e6"];
        cv4 [label="CV_4 — Color (bright/dark)\\nTilt + harmonic family\\nCCW: darker / even partials\\nCW: brighter / odd partials" shape=rect fillcolor="#fff7e6"];
        cv5 [label="CV_5 — V/OCT (0–10 V)\\nSets musical fundamental\\nC0 (0 V) up to high pitches" shape=rect fillcolor="#fff7e6"];
        cv6 [label="CV_6 — Time‑stretch / density\\nBipolar around 1× time\\nSlow clouds ↔ dense motion" shape=rect fillcolor="#fff7e6"];
        cv7 [label="CV_7 — SPARSITY\\nSelects only strongest bins\\nfor metallic / chime‑like tones" shape=rect fillcolor="#fff7e6"];
        cv8 [label="CV_8 — Phase diffusion\\nRandomizes phases for\\nnoisy, cloud‑like textures\\n(FLUFF can add extra)" shape=rect fillcolor="#fff7e6"];

        b7 [label="B_7 — MAX COMP\\nToggle strong output\\ncompressor mode" shape=rect fillcolor="#e8f7ff"];
        b8 [label="B_8 — Mode select\\nPitch‑locked grains (ON)\\nvs partial‑based /\\nspectral model (OFF)" shape=rect fillcolor="#e8f7ff"];
    }

    // Control routing to audio path
    cv1 -> grains  [label="Send + dry/wet into\\nresynth grain engine" fontsize=8];
    cv2 -> grains  [label="Time‑stretch / density" fontsize=8];
    cv3 -> spectral [label="FLUFF stages:\\ncloud depth" fontsize=8];
    cv4 -> spectral [label="Bright / dark tilt\\n+ harmonic family" fontsize=8];
    cv5 -> pvoc     [label="V/OCT fundamental" fontsize=8];
    cv6 -> grains   [label="Time‑stretch / density" fontsize=8];
    cv7 -> spectral [label="Sparsity threshold" fontsize=8];
    cv8 -> spectral [label="Phase diffusion" fontsize=8];

    b7 -> comp      [label="Toggle MAX COMP\\ncompressor mode" fontsize=8];
    b8 -> pvoc      [label="Mode: pitch‑locked\\ngrains vs partial‑based" fontsize=8];
}
"""


def main() -> None:
    here = Path(__file__).resolve().parent
    dot_path = here / "Resynthesis_BlockDiagram.dot"
    pdf_path = here / "Resynthesis_BlockDiagram_USLetter.pdf"
    svg_path = here / "Resynthesis_BlockDiagram_USLetter.svg"

    dot_path.write_text(DOT_SOURCE.strip() + "\n", encoding="utf-8")

    for fmt, out_path in (("pdf", pdf_path), ("svg", svg_path)):
        try:
            subprocess.run(
                ["dot", f"-T{fmt}", str(dot_path), "-o", str(out_path)],
                check=True,
            )
            print(f"wrote {out_path}")
        except FileNotFoundError:
            print("error: graphviz 'dot' command not found on PATH.")
            print("please install Graphviz (https://graphviz.org) and re-run.")
            break
        except subprocess.CalledProcessError as exc:
            print(f"dot failed with exit code {exc.returncode} for format {fmt}")
            break


if __name__ == "__main__":
    main()

