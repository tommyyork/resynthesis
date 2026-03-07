# Project Name
TARGET = Resynthesis

USE_DAISYSP_LGPL = 1

# Sources
CPP_SOURCES = Resynthesis.cpp \
              Compression.cpp \
              Shifting.cpp \

# Library Locations (use local submodules)
LIBDAISY_DIR = ./libDaisy
DAISYSP_DIR = ./DaisySP

# Ensure libraries are built before building this project
# `all` builds:
# - libDaisy and DaisySP
# - the firmware target (from the core Makefile)
# - host test binaries (via the `test` subdirectory)
# - panel preview PNGs
# - SVG/PDF block-diagram documentation
all: build_libdaisy build_daisysp panel svg test_binaries

build_libdaisy:
	$(MAKE) -C $(LIBDAISY_DIR)

build_daisysp:
	$(MAKE) -C $(DAISYSP_DIR)


# Core location, and generic Makefile.
SYSTEM_FILES_DIR = $(LIBDAISY_DIR)/core
include $(SYSTEM_FILES_DIR)/Makefile

# DFU programming targets.
# `_program-dfu` is the strict, low-level DFU target: it builds everything
# (via the `all` prerequisite) and then invokes `dfu-util` directly. Any
# error from `dfu-util` (including the common "Error during download get_status"
# that can occur when the device reboots immediately after flashing) will cause
# this target to fail.
#
# `program-dfu` is the relaxed convenience wrapper intended for day-to-day
# development. It calls `_program-dfu` but ignores its exit status, so
# successful flashes that trigger a spurious `get_status` error are treated
# as success from the Make/VS Code point of view.
_program-dfu: all
	dfu-util -a 0 -s $(FLASH_ADDRESS):leave -D $(BUILD_DIR)/$(TARGET_BIN) -d ,0483:$(USBPID)

program-dfu:
	-$(MAKE) _program-dfu

# Panel previews: generate PNGs from the SVG panel and Eurorack overlay.
# Requires Inkscape. Set INKSCAPE=/path/to/inkscape if not in PATH (e.g. macOS: /Applications/Inkscape.app/Contents/MacOS/inkscape).
INKSCAPE ?= inkscape
PANEL_OUTPUT_DIR := panel/output

.PHONY: panel svg tests samples fluff test_resynth_props test_panel test_voct resynthesis-clean clean

# Panel: generate KiCad panel PCB and footprints.
panel:
	$(PYTHON) panel/generate_panel_kicad.py

# Offline tests:
# - Property / stability tests (no WAVs) intended for automation.
# - Sample renders (WAVs under test/out/) for subjective listening.
# Panel test: run panel alignment / mechanical checks (Python).
PYTHON ?= python3
DOC_DIR := doc

# Build-only target for host test binaries (does not run tests).
test_binaries:
	$(MAKE) -C test all

# Firmware size guard: ensure the embedded firmware stays within SRAM limits
# for the target platform. This uses the linker map file produced by the
# libDaisy core Makefile.
firmware_size: $(BUILD_DIR)/$(TARGET).map
	$(PYTHON) test/check_firmware_size.py $(BUILD_DIR)/$(TARGET).map

# Automated tests: property checks, V/OCT harmonic tests (via `voct` target),
# panel alignment, and firmware size guard.
tests: test_resynth_props voct panel test_panel firmware_size

test_resynth_props:
	$(MAKE) -C test tests

# Sample render suite: runs all offline sample-processing programs that
# generate WAVs under test/out/ for subjective evaluation.
samples:
	$(MAKE) -C test samples

# FLUFF-only renders: run just the FLUFF offline sample-processing program,
# generating WAVs under test/out/fluff/ for subjective evaluation.
.PHONY: fluff
fluff:
	$(MAKE) -C test fluff

# V/OCT harmonic analysis: OneShotOneOsc.wav -> test/out/voct_harmonic/*.csv, *.svg
test_voct:
	$(MAKE) -C test voct

.PHONY: voct
voct: test_voct

# Documentation: US Letter–sized block diagram of the audio path and controls.
# Invoke from the Resynthesis module root:
#   make svg
.PHONY: svg
svg:
	$(PYTHON) $(DOC_DIR)/generate_block_diagram.py

resynthesis-clean:
	rm -rf build
	rm -rf test/out
	rm -rf panel/output
	rm -f $(DOC_DIR)/Resynthesis_BlockDiagram_USLetter.pdf \
	      $(DOC_DIR)/Resynthesis_BlockDiagram_USLetter.svg \
	      $(DOC_DIR)/Resynthesis_BlockDiagram.dot
	$(MAKE) -C test clean

clean: resynthesis-clean
