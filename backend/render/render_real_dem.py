"""
Render the DEM view a PTZ camera would see, using real elevation data.

Setup (one-time):
    pip install numpy pandas matplotlib rasterio requests

Choose a DEM source:
  A) OpenTopography (worldwide, free key required)
       1. Get a key at https://portal.opentopography.org/myopentopo
       2. export OPENTOPO_API_KEY=...
  B) py3dep (USGS 3DEP, USA only, no key)
       pip install py3dep
  C) Local GeoTIFF you already have on disk

Then run:
    python render_real_dem.py
"""
import os
import math
import pandas as pd
import matplotlib
from pathlib import Path
matplotlib.use("Agg")

from dem_camera_view import (
    Camera, FT_TO_M,
    camera_from_csv_row, render_camera_view, display_render, bbox_around,
    download_dem_opentopography, download_dem_py3dep, load_dem_geotiff,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
CSV_PATH        = f"{DATA_DIR}/camera_parameters.csv"
OUT_DIR         = f"{ROOT}/backend/render/out"
HALF_EXTENT_KM  = 3.0           # DEM coverage radius around camera
MAX_DIST_KM     = 15.0           # how far out to render

os.makedirs(OUT_DIR, exist_ok=True)
df = pd.read_csv(CSV_PATH)

# All rows in this CSV share the same camera, so we download the DEM once.
cam_lat = float(df.camera_lat.iloc[0])
cam_lon = float(df.camera_lon.iloc[0])
W, S, E, N = bbox_around(cam_lat, cam_lon, half_extent_km=HALF_EXTENT_KM)
print(f"DEM bbox: W={W:.4f}  S={S:.4f}  E={E:.4f}  N={N:.4f}")

# ---------------------------------------------------------------- #
# Pick ONE of these DEM sources:
# ---------------------------------------------------------------- #

# (A) OpenTopography — needs OPENTOPO_API_KEY. Worldwide.
#elev, dem_bounds = download_dem_opentopography(W, S, E, N, demtype="SRTMGL1")

# (B) USGS 3DEP via py3dep (USA only, no key):
elev, dem_bounds = download_dem_py3dep(W, S, E, N, resolution_m=10)

# (C) Local GeoTIFF you have on disk:
# elev, dem_bounds = load_dem_geotiff("path/to/your_dem.tif")

print(f"DEM loaded: {elev.shape}  elevation {elev.min():.0f}–{elev.max():.0f} m")

# ---------------------------------------------------------------- #
# Render every frame in the CSV
# ---------------------------------------------------------------- #
for _, row in df.iterrows():
    cam = camera_from_csv_row(row)
    # Higher zoom → smaller FOV → upsample DEM more for a clean image.
    if   cam.zoom < 3:    upsample = 1
    elif cam.zoom < 6:    upsample = 2
    elif cam.zoom < 12:   upsample = 4
    elif cam.zoom < 25:   upsample = 8
    else:                 upsample = 16

    print(f"frame {row.frame_nr:>4}  pan={cam.pan_deg:6.2f}°  "
          f"tilt={cam.tilt_deg:6.2f}°  zoom={cam.zoom:5.2f}×  "
          f"FOV={cam.fov_h_deg:5.2f}°  upsample={upsample}")

    img, _ = render_camera_view(
        cam, elev, dem_bounds,
        dem_upsample=upsample,
        max_distance_km=MAX_DIST_KM,
    )
    out_path = os.path.join(OUT_DIR, f"frame_{int(row.frame_nr):04d}.png")
    display_render(img, cam,
                   title=f"{row.camera_name}  frame {row.frame_nr}  |  "
                         f"pan={cam.pan_deg:.1f}°  "
                         f"tilt={cam.tilt_deg:.1f}°  "
                         f"zoom={cam.zoom:.1f}×  "
                         f"(FOV ≈ {cam.fov_h_deg:.1f}°)",
                   output_path=out_path)
    matplotlib.pyplot.close("all")

print(f"\nDone. Rendered images in {OUT_DIR}/")
