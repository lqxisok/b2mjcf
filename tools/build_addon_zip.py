#!/usr/bin/env python3
"""Build the installable Blender add-on archive."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "blender_mjcf_exporter_45"
OUTPUT = ROOT / "dist" / "blender_mjcf_exporter_45_addon.zip"


def main() -> None:
    if not (PACKAGE / "__init__.py").is_file():
        raise SystemExit(f"Missing add-on package: {PACKAGE}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(PACKAGE.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(ROOT).as_posix())
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
