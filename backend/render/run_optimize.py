"""
Run pose optimisation for each frame that has both a real image and a CSV row.

Usage:
    python run_optimize.py              # optimise all matching frames
    python run_optimize.py 0014 0043    # optimise specific frames only
"""
import os
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")

from pathlib import Path
from PIL import Image

from dem_camera_view import (
    Camera, FT_TO_M,
    camera_from_csv_row, render_camera_view, display_render, bbox_around,
    download_dem_py3dep,
)
from optimize_pose import (
    optimize_pose, extract_edges, overlay_edges, save_comparison,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
CSV_PATH = DATA_DIR / "camera_parameters.csv"
OUT_DIR = ROOT / "backend" / "render" / "optimized"
HALF_EXTENT_KM = 3.0
MAX_DIST_KM = 15.0

os.makedirs(OUT_DIR, exist_ok=True)

# ---- Load CSV and DEM ------------------------------------------------ #
df = pd.read_csv(CSV_PATH)
cam_lat = float(df.camera_lat.iloc[0])
cam_lon = float(df.camera_lon.iloc[0])
W, S, E, N = bbox_around(cam_lat, cam_lon, half_extent_km=HALF_EXTENT_KM)
print(f"DEM bbox: W={W:.4f}  S={S:.4f}  E={E:.4f}  N={N:.4f}")

elev, dem_bounds = download_dem_py3dep(W, S, E, N, resolution_m=10)
print(f"DEM loaded: {elev.shape}  elevation {elev.min():.0f}-{elev.max():.0f} m")

# ---- Find frames to process ----------------------------------------- #
real_images = {p.stem: p for p in DATA_DIR.glob("frame_*.jpg")}

# Optional CLI filter
if len(sys.argv) > 1:
    requested = {f"frame_{x.zfill(4)}" for x in sys.argv[1:]}
    real_images = {k: v for k, v in real_images.items() if k in requested}

print(f"Found {len(real_images)} real images")

# ---- Optimise each frame --------------------------------------------- #
results_rows = []

for _, row in df.iterrows():
    stem = f"frame_{int(row.frame_nr):04d}"
    if stem not in real_images:
        continue

    print(f"\n{'='*60}")
    print(f"Optimising {stem}")
    print(f"{'='*60}")

    cam = camera_from_csv_row(row)

    # Determine DEM upsample factor from zoom level.
    if   cam.zoom < 3:    upsample = 1
    elif cam.zoom < 6:    upsample = 2
    elif cam.zoom < 12:   upsample = 4
    elif cam.zoom < 25:   upsample = 8
    else:                 upsample = 16

    # Load real image.
    real_pil = Image.open(real_images[stem]).convert("RGB")
    real_np = np.array(real_pil)

    render_kw = dict(dem_upsample=upsample, max_distance_km=MAX_DIST_KM)

    # --- Before optimisation render ----------------------------------- #
    before_img, _ = render_camera_view(cam, elev, dem_bounds, **render_kw)
    before_rgb = (np.clip(before_img, 0, 1) * 255).astype(np.uint8)

    # --- Run optimisation --------------------------------------------- #
    opt_cam, history = optimize_pose(
        cam, real_np, elev, dem_bounds,
        max_iters=3,
        convergence_px=0.5,
        render_kwargs=render_kw,
        verbose=True,
    )

    # --- After optimisation render ------------------------------------ #
    after_img, _ = render_camera_view(opt_cam, elev, dem_bounds, **render_kw)
    after_rgb = (np.clip(after_img, 0, 1) * 255).astype(np.uint8)

    # --- Save outputs ------------------------------------------------- #
    # 1. Side-by-side edge comparison (before).
    save_comparison(real_np, before_rgb,
                    str(OUT_DIR / f"{stem}_before.jpg"))

    # 2. Side-by-side edge comparison (after).
    save_comparison(real_np, after_rgb,
                    str(OUT_DIR / f"{stem}_after.jpg"))

    # 3. Optimised render.
    display_render(after_img, opt_cam,
                   title=f"{stem} optimised  |  "
                         f"pan={opt_cam.pan_deg:.2f}  "
                         f"tilt={opt_cam.tilt_deg:.2f}  "
                         f"roll={opt_cam.roll_deg:.2f}  "
                         f"fov={opt_cam.fov_h_deg:.2f}",
                   output_path=str(OUT_DIR / f"{stem}_render.png"))
    matplotlib.pyplot.close("all")

    # Collect summary.
    last = history[-1] if history else None
    results_rows.append(dict(
        frame=stem,
        orig_pan=cam.pan_deg, orig_tilt=cam.tilt_deg,
        opt_pan=opt_cam.pan_deg, opt_tilt=opt_cam.tilt_deg,
        opt_roll=opt_cam.roll_deg,
        opt_fov_wide=opt_cam.wide_fov_deg,
        cost=last.cost if last else None,
        converged=last.converged if last else False,
        iters=len(history),
    ))

    print(f"  Original:  pan={cam.pan_deg:.2f}  tilt={cam.tilt_deg:.2f}")
    print(f"  Optimised: pan={opt_cam.pan_deg:.2f}  tilt={opt_cam.tilt_deg:.2f}  "
          f"roll={opt_cam.roll_deg:.2f}  fov_wide={opt_cam.wide_fov_deg:.2f}")

# ---- Summary --------------------------------------------------------- #
if results_rows:
    results_df = pd.DataFrame(results_rows)
    results_df.to_csv(OUT_DIR / "optimization_results.csv", index=False)
    print(f"\nResults saved to {OUT_DIR / 'optimization_results.csv'}")
    print(results_df.to_string(index=False))

print(f"\nDone. Outputs in {OUT_DIR}/")
