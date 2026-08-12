#!/usr/bin/env python3
"""Batch-extract LabVIEW reconstruction evidence for a module.

For every VI in a source folder this script:

1. Runs the pylabview ``readRSRC`` extractor to produce XML datasets with
   control/indicator names, type descriptors, connector info, and defaults.
2. Dumps printable strings from the VI binary (protocol constants, error
   messages, serial settings, register names).
3. Writes everything under ``reconstructions/<module>/<VI basename>/`` so
   each module report can cite exact artifacts.

Usage:
    python tools/extract_vi.py Labview reconstructions/labview
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PYTHON = sys.executable


def dump_strings(source: Path, output: Path) -> None:
    """Extract printable ASCII runs >= 5 chars (labels, constants, errors)."""
    data = source.read_bytes()
    matches = re.findall(rb"[\x20-\x7e]{5,}", data)
    with open(output, "w", encoding="utf-8") as handle:
        for match in matches:
            handle.write(match.decode("ascii") + "\n")


def extract_vi(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    xml = destination / (source.stem.replace(" ", "_") + ".xml")
    if not xml.exists():
        subprocess.run(
            [PYTHON, "-m", "pylabview.readRSRC", "-x",
             "-i", str(source), "-m", str(xml)],
            check=False,
            capture_output=True,
        )
    # readRSRC writes raw binary sidecars (VICD code, icons, ...). The XML
    # dumps already contain the parsed content, so drop the regenerable
    # sidecars to keep the repository small.
    for sidecar in destination.glob("*"):
        if sidecar.suffix in {".bin", ".png"}:
            sidecar.unlink(missing_ok=True)
    strings = destination / "strings.txt"
    if not strings.exists():
        dump_strings(source, strings)
    if xml.stat().st_size < 1024:
        print(f"  !! sparse evidence for {source.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="VI file or folder of VIs")
    parser.add_argument(
        "destination", help="output folder (e.g. reconstructions/labview)"
    )
    args = parser.parse_args()

    source = Path(args.source)
    destination = Path(args.destination)
    files: list[Path] = []
    if source.is_file():
        files = [source]
    else:
        files = sorted(
            path for path in source.rglob("*")
            if path.suffix.lower() in {".vi", ".ctl"} and path.is_file()
        )
    for vi in files:
        print(f"extracting {vi.relative_to(source)} ...")
        extract_vi(vi, destination / vi.parent.relative_to(source) / vi.stem.replace(" ", "_"))
    print(f"done: {len(files)} files -> {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
