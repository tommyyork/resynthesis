#!/usr/bin/env python3
"""
Size guard for the Resynthesis firmware.

This script parses the linker map file and enforces that the SRAM usage
(.data + .bss placed in the SRAM region) stays within the target's limits.

Intended to be run from the Resynthesis module root via:

    make tests

and is wired into the top-level Makefile.
"""

import pathlib
import re
import sys


SRAM_BYTES = 0x80000  # 512 KiB on Daisy Patch SM (STM32H750)


def parse_section_sizes_from_map(map_path: pathlib.Path):
    """Return (.data_size, .bss_size) in bytes from a GNU ld map file."""
    data_size = None
    bss_size = None

    data_re = re.compile(r"^\.data\s+0x[0-9a-fA-F]+\s+0x([0-9a-fA-F]+)")
    bss_re = re.compile(r"^\.bss\s+0x[0-9a-fA-F]+\s+0x([0-9a-fA-F]+)")

    with map_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if data_size is None:
                m = data_re.match(line)
                if m:
                    data_size = int(m.group(1), 16)
                    continue
            if bss_size is None:
                m = bss_re.match(line)
                if m:
                    bss_size = int(m.group(1), 16)
                    continue
            if data_size is not None and bss_size is not None:
                break

    if data_size is None or bss_size is None:
        raise RuntimeError(f"Failed to find .data/.bss sizes in {map_path}")

    return data_size, bss_size


def main(argv):
    if len(argv) > 2:
        print(f"Usage: {argv[0]} [path/to/Resynthesis.map]", file=sys.stderr)
        return 2

    if len(argv) == 2:
        map_path = pathlib.Path(argv[1])
    else:
        # Default: invoked from Resynthesis root, map lives under build/
        map_path = pathlib.Path("build/Resynthesis.map")

    if not map_path.is_file():
        print(f"error: linker map file not found: {map_path}", file=sys.stderr)
        return 1

    data_size, bss_size = parse_section_sizes_from_map(map_path)
    sram_used = data_size + bss_size

    print(f".data = {data_size} bytes, .bss = {bss_size} bytes, "
          f"SRAM total = {sram_used} bytes (limit {SRAM_BYTES} bytes)")

    if sram_used > SRAM_BYTES:
        over = sram_used - SRAM_BYTES
        print(
            f"error: firmware SRAM usage exceeds target limit by "
            f"{over} bytes (SRAM={SRAM_BYTES}, used={sram_used})",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

