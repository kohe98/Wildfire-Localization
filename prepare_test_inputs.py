"""
Reads data/camera_parameters.csv and data/frame_XXXX.jpg files and produces:

  test_inputs/
    frame_0014/
      image.jpg
      metadata.json
    frame_0043/
      ...

Run with:  python prepare_test_inputs.py
"""

import ast
import csv
import json
import shutil
from pathlib import Path

DATA_DIR = Path("data")
OUT_DIR = Path("test_inputs")
CSV_PATH = DATA_DIR / "camera_parameters.csv"


def parse_pixel_coord(raw: str) -> list[int]:
    """Convert '(1381,596)' → [1381, 596]."""
    x, y = ast.literal_eval(raw.strip())
    return [int(x), int(y)]


with open(CSV_PATH, newline="") as f:
    reader = csv.DictReader(f)
    for row in reader:
        frame_nr = int(row["frame_nr"])
        folder_name = f"frame_{frame_nr:04d}"
        out_dir = OUT_DIR / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        # Copy image
        src_image = DATA_DIR / f"frame_{frame_nr:04d}.jpg"
        dst_image = out_dir / "image.jpg"
        if src_image.exists():
            shutil.copy2(src_image, dst_image)
        else:
            print(f"WARNING: {src_image} not found, skipping image copy")

        # Write metadata
        metadata = {
            "camera_name": row["camera_name"],
            "camera_lat": float(row["camera_lat"]),
            "camera_lon": float(row["camera_lon"]),
            "camera_elev_m": float(row["elev"]),
            "pan": float(row["x"]),
            # ALERT CSV uses +y = down; renderer uses -tilt = down, so negate
            "tilt": -float(row["y"]),
            "zoom": float(row["z"]),
            "smoke_bbox": {
                "top_left": parse_pixel_coord(row["smoke_pixel_coord_tl"]),
                "bottom_right": parse_pixel_coord(row["smoke_pixel_coord_br"]),
            },
            "ground_truth": {
                "fire_lat": float(row["fire_lat"]),
                "fire_lon": float(row["fire_lon"]),
            },
        }

        with open(out_dir / "metadata.json", "w") as mf:
            json.dump(metadata, mf, indent=2)

        print(f"Created {out_dir}/")

print(f"\nDone. {len(list(OUT_DIR.iterdir()))} events in {OUT_DIR}/")
