import json
import math
import os
import shutil
import sys
import uuid
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # no display needed — must be set before importing pyplot

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image as PILImage
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent / "render"))
from dem_camera_view import (
    Camera, FT_TO_M, EARTH_RADIUS_M,
    render_camera_view, bbox_around, download_dem_py3dep,
    geodetic_to_enu,
)

app = FastAPI(title="Wildfire Localization API")

cors_origins = os.getenv("CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)
PRECOMPUTED_DIR = Path(__file__).parent / "precomputed"
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
app.mount("/precomputed", StaticFiles(directory=PRECOMPUTED_DIR), name="precomputed")

latest_event: dict | None = None


def load_precomputed(frame_id: str) -> dict:
    """Load a pre-rendered event from disk. Raises FileNotFoundError if unknown."""
    frame_dir = PRECOMPUTED_DIR / frame_id
    ev = json.loads((frame_dir / "event.json").read_text())
    # Shared elevation grid (same DEM area for all frames)
    ev["elevation_grid"] = np.load(PRECOMPUTED_DIR / "elevation_grid.npy").tolist()
    return ev


# Render at a wider FOV than the actual camera zoom so the projection
# always contains the terrain visible in the photo, even if pan/tilt are
# slightly off. Value of 2.0 = double the normal FOV (half the zoom).
RENDER_FOV_SCALE = 3.0


def upsample_for_zoom(zoom: float) -> int:
    if zoom < 3:   return 1
    elif zoom < 6:  return 2
    elif zoom < 12: return 4
    elif zoom < 25: return 8
    else:           return 16


# ---------------------------------------------------------------------------
# Ray casting
# ---------------------------------------------------------------------------

def _enu_to_geodetic(east, north, cam_lat, cam_lon):
    """Convert ENU offset (m) back to lat/lon."""
    lat = cam_lat + math.degrees(north / EARTH_RADIUS_M)
    lon = cam_lon + math.degrees(east / (EARTH_RADIUS_M * math.cos(math.radians(cam_lat))))
    return lat, lon


def _sample_dem(lat, lon, dem_elev, dem_bounds):
    """Bilinearly sample the DEM at (lat, lon). Returns None if out of bounds."""
    west, south, east, north = dem_bounds
    if not (west <= lon <= east and south <= lat <= north):
        return None
    h, w = dem_elev.shape
    fx = (lon - west) / (east - west) * (w - 1)
    fy = (north - lat) / (north - south) * (h - 1)
    x0, y0 = int(fx), int(fy)
    x1, y1 = min(x0 + 1, w - 1), min(y0 + 1, h - 1)
    tx, ty = fx - x0, fy - y0
    return float(
        (1 - ty) * ((1 - tx) * dem_elev[y0, x0] + tx * dem_elev[y0, x1]) +
             ty  * ((1 - tx) * dem_elev[y1, x0] + tx * dem_elev[y1, x1])
    )


def cast_ray_intersections(
    cam: Camera,
    proj_pixel_u: float,
    proj_pixel_v: float,
    dem_elev: np.ndarray,
    dem_bounds: tuple,
    max_distance_m: float = 15_000.0,
    step_m: float = 10.0,
) -> list[dict]:
    """
    Cast a ray from the camera through projection pixel (u, v) and return all
    terrain intersections, ordered nearest to farthest.

    Each intersection: {lat, lon, distance_m}
    """
    # Unproject pixel → direction in camera space (X=right, Y=down, Z=forward)
    cx = (proj_pixel_u - cam.img_w / 2.0) / cam.focal_px
    cy = (proj_pixel_v - cam.img_h / 2.0) / cam.focal_px
    cz = 1.0
    cam_dir = np.array([cx, cy, cz])
    cam_dir /= np.linalg.norm(cam_dir)

    # Rotate to ENU world space (R maps ENU → cam, so R.T maps cam → ENU)
    # ENU: X=East, Y=North, Z=Up
    enu_dir = cam.R.T @ cam_dir  # (east, north, up) direction

    intersections = []
    prev_above = True  # start above terrain (at camera position)

    steps = int(max_distance_m / step_m)
    for i in range(1, steps + 1):
        dist = i * step_m
        east  = enu_dir[0] * dist
        north = enu_dir[1] * dist
        up    = enu_dir[2] * dist

        ray_lat, ray_lon = _enu_to_geodetic(east, north, cam.lat, cam.lon)
        ray_alt = cam.alt_m + up

        terrain_alt = _sample_dem(ray_lat, ray_lon, dem_elev, dem_bounds)
        if terrain_alt is None:
            break  # left the DEM coverage area

        currently_above = ray_alt >= terrain_alt

        if prev_above and not currently_above:
            # Crossed the surface — refine with linear interpolation
            prev_dist = (i - 1) * step_m
            t = (cam.alt_m + enu_dir[2] * prev_dist - terrain_alt) / (
                (cam.alt_m + enu_dir[2] * prev_dist) - (ray_alt) +
                (terrain_alt - _sample_dem(
                    *_enu_to_geodetic(enu_dir[0] * prev_dist, enu_dir[1] * prev_dist, cam.lat, cam.lon),
                    dem_elev, dem_bounds
                ) or terrain_alt)
            )
            hit_dist = prev_dist + t * step_m
            hit_east  = enu_dir[0] * hit_dist
            hit_north = enu_dir[1] * hit_dist
            hit_lat, hit_lon = _enu_to_geodetic(hit_east, hit_north, cam.lat, cam.lon)
            intersections.append({
                "lat": round(hit_lat, 6),
                "lon": round(hit_lon, 6),
                "distance_m": round(hit_dist, 1),
            })

        prev_above = currently_above

    return intersections


def invert_user_transform(
    sx: float, sy: float,
    dx: float, dy: float,
    scale: float, rotation_deg: float,
    img_w: int, img_h: int,
) -> tuple[float, float]:
    """
    The user applied a 2D similarity transform to the projection image:
        camera_pixel = scale * R(rotation) * proj_pixel + (dx, dy)
    where the rotation is about the image centre.

    Invert this to find which projection pixel corresponds to camera pixel (sx, sy).
    """
    cx, cy = img_w / 2.0, img_h / 2.0
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    # Translate so rotation is about origin
    sx -= cx + dx
    sy -= cy + dy

    # Undo scale and rotation
    pu = (cos_t * sx + sin_t * sy) / scale + cx
    pv = (-sin_t * sx + cos_t * sy) / scale + cy

    return pu, pv


# ---------------------------------------------------------------------------
# Bird's eye overview
# ---------------------------------------------------------------------------

def render_birds_eye_view(
    cam: Camera,
    dem_elev: np.ndarray,
    dem_bounds: tuple,
    enu_dir: np.ndarray,
    intersections: list[dict],
    ground_truth: dict | None = None,
    baseline: dict | None = None,
) -> str:
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource

    west, south, east, north = dem_bounds

    fig, ax = plt.subplots(figsize=(10, 8), dpi=110)

    dem_clean = np.where(np.isnan(dem_elev), np.nanmean(dem_elev), dem_elev)
    ls = LightSource(azdeg=315, altdeg=45)
    rgb = ls.shade(dem_clean, cmap=plt.get_cmap("terrain"),
                   blend_mode="soft", vert_exag=2.0)
    ax.imshow(rgb, extent=[west, east, south, north], origin="upper", aspect="equal")

    ax.plot(cam.lon, cam.lat, "^", color="black", markersize=14,
            markerfacecolor="yellow", markeredgewidth=2, zorder=5, label="Camera")

    max_dist = 3000.0
    if intersections:
        max_dist = max(max_dist, max(c["distance_m"] for c in intersections) * 1.5)
    ray_lons, ray_lats = [cam.lon], [cam.lat]
    for i in range(1, 201):
        d = i * max_dist / 200
        lat, lon = _enu_to_geodetic(enu_dir[0] * d, enu_dir[1] * d, cam.lat, cam.lon)
        ray_lons.append(lon)
        ray_lats.append(lat)
    ax.plot(ray_lons, ray_lats, "-", color="red", linewidth=2.5, zorder=4, label="Ray")

    for idx, c in enumerate(intersections):
        ax.plot(c["lon"], c["lat"], "o", color="red", markersize=13,
                markeredgecolor="white", markeredgewidth=2.5, zorder=6)
        ax.annotate(f"#{idx + 1}", (c["lon"], c["lat"]),
                    textcoords="offset points", xytext=(10, 10),
                    fontsize=11, fontweight="bold", color="darkred",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                              edgecolor="red", alpha=0.9),
                    zorder=7)

    if baseline:
        ax.plot(baseline["lon"], baseline["lat"], "D", color="steelblue", markersize=12,
                markeredgecolor="white", markeredgewidth=2, zorder=6, label="Unaligned baseline")

    if ground_truth:
        ax.plot(ground_truth["fire_lon"], ground_truth["fire_lat"], "*",
                color="limegreen", markersize=18, markeredgecolor="darkgreen",
                markeredgewidth=1.5, zorder=6, label="Ground Truth")

    all_lats = [cam.lat] + [c["lat"] for c in intersections]
    all_lons = [cam.lon] + [c["lon"] for c in intersections]
    if baseline:
        all_lats.append(baseline["lat"])
        all_lons.append(baseline["lon"])
    if ground_truth:
        all_lats.append(ground_truth["fire_lat"])
        all_lons.append(ground_truth["fire_lon"])
    lat_span = max(all_lats) - min(all_lats)
    lon_span = max(all_lons) - min(all_lons)
    pad = max(lat_span, lon_span) * 0.4 + 0.003
    ax.set_xlim(min(all_lons) - pad, max(all_lons) + pad)
    ax.set_ylim(min(all_lats) - pad, max(all_lats) + pad)

    ax.ticklabel_format(useOffset=False, style="plain")
    ax.legend(loc="upper right", fontsize=11, framealpha=0.9)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Bird's Eye View", fontsize=14)
    fig.tight_layout()

    filename = f"{uuid.uuid4()}_overview.png"
    fig.savefig(UPLOADS_DIR / filename, dpi=110, bbox_inches="tight", pad_inches=0.15)
    matplotlib.pyplot.close(fig)

    return f"/uploads/{filename}"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

class LocalizeRequest(BaseModel):
    smoke_pixel: list[float]    # [u, v] in camera image coords
    dx: float                   # horizontal translation applied by user (px)
    dy: float                   # vertical translation applied by user (px)
    scale: float                # scale applied by user
    rotation_deg: float         # rotation applied by user (degrees)


@app.post("/api/event")
async def receive_event(
    image: UploadFile = File(...),
    metadata: str = Form(...),
):
    meta = json.loads(metadata)

    image_filename = f"{uuid.uuid4()}.jpg"
    image_path = UPLOADS_DIR / image_filename
    with image_path.open("wb") as f:
        shutil.copyfileobj(image.file, f)

    # elev in the CSV is in feet despite our field being named camera_elev_m
    actual_zoom = meta["zoom"]
    render_zoom = actual_zoom / RENDER_FOV_SCALE

    cam = Camera(
        lat=meta["camera_lat"],
        lon=meta["camera_lon"],
        alt_m=meta["camera_elev_m"] * FT_TO_M,
        pan_deg=meta["pan"],
        tilt_deg=meta["tilt"],
        zoom=render_zoom,
    )

    west, south, east, north = bbox_around(cam.lat, cam.lon, half_extent_km=6.0)
    dem_elev, dem_bounds = download_dem_py3dep(west, south, east, north, resolution_m=30)

    img_array, _ = render_camera_view(
        cam, dem_elev, dem_bounds,
        dem_upsample=upsample_for_zoom(actual_zoom),
        max_distance_km=15.0,
    )

    projection = PILImage.fromarray(
        (np.clip(img_array, 0, 1) * 255).astype(np.uint8)
    )

    projection_filename = f"{uuid.uuid4()}_projection.jpg"
    projection.save(UPLOADS_DIR / projection_filename)

    global latest_event
    latest_event = {
        "camera_image_url": f"/uploads/{image_filename}",
        "projection_image_url": f"/uploads/{projection_filename}",
        "metadata": meta,
        "render_zoom": render_zoom,
        "render_fov_scale": RENDER_FOV_SCALE,
        "bbox": {"west": west, "south": south, "east": east, "north": north},
        "elevation_grid": dem_elev.tolist(),
        "dem_bounds": list(dem_bounds),
    }

    return {
        "status": "ok",
        "camera_image_url": latest_event["camera_image_url"],
        "projection_image_url": latest_event["projection_image_url"],
    }


@app.post("/api/localize")
def localize(req: LocalizeRequest):
    if latest_event is None:
        raise HTTPException(status_code=404, detail="No event received yet")

    meta = latest_event["metadata"]
    dem_elev = np.array(latest_event["elevation_grid"], dtype=np.float32)
    dem_bounds = tuple(latest_event["dem_bounds"])
    render_zoom = latest_event["render_zoom"]

    cam = Camera(
        lat=meta["camera_lat"],
        lon=meta["camera_lon"],
        alt_m=meta["camera_elev_m"] * FT_TO_M,
        pan_deg=meta["pan"],
        tilt_deg=meta["tilt"],
        zoom=render_zoom,
    )

    # Map smoke pixel in camera image → pixel in unaligned projection
    proj_u, proj_v = invert_user_transform(
        sx=req.smoke_pixel[0],
        sy=req.smoke_pixel[1],
        dx=req.dx,
        dy=req.dy,
        scale=req.scale,
        rotation_deg=req.rotation_deg,
        img_w=cam.img_w,
        img_h=cam.img_h,
    )

    intersections = cast_ray_intersections(cam, proj_u, proj_v, dem_elev, dem_bounds)

    if not intersections:
        raise HTTPException(status_code=422, detail="Ray did not intersect terrain")

    # Unaligned baseline: cast a ray with no user transform, using the bottom
    # center of the smoke bbox (more likely to be at ground level than the center)
    render_fov_scale = latest_event.get("render_fov_scale", RENDER_FOV_SCALE)
    bbox = meta.get("smoke_bbox")
    if bbox:
        base_sx = (bbox["top_left"][0] + bbox["bottom_right"][0]) / 2.0
        base_sy = bbox["bottom_right"][1]
    else:
        base_sx, base_sy = req.smoke_pixel[0], req.smoke_pixel[1]
    base_u, base_v = invert_user_transform(
        sx=base_sx, sy=base_sy,
        dx=0, dy=0, scale=render_fov_scale, rotation_deg=0,
        img_w=cam.img_w, img_h=cam.img_h,
    )
    baseline_hits = cast_ray_intersections(cam, base_u, base_v, dem_elev, dem_bounds)
    baseline = baseline_hits[0] if baseline_hits else None

    cx = (proj_u - cam.img_w / 2.0) / cam.focal_px
    cy = (proj_v - cam.img_h / 2.0) / cam.focal_px
    cam_dir = np.array([cx, cy, 1.0])
    cam_dir /= np.linalg.norm(cam_dir)
    enu_dir = cam.R.T @ cam_dir

    overview_url = render_birds_eye_view(
        cam, dem_elev, dem_bounds, enu_dir, intersections,
        ground_truth=meta.get("ground_truth"),
        baseline=baseline,
    )

    return {
        "candidates": intersections,
        "ground_truth": meta.get("ground_truth"),
        "baseline": baseline,
        "overview_image_url": overview_url,
    }


@app.get("/api/event/latest")
def get_latest_event():
    if latest_event is None:
        raise HTTPException(status_code=404, detail="No event received yet")
    return latest_event


@app.post("/api/event/preset/{frame_id}")
def set_preset_event(frame_id: str):
    """Switch the current event to a pre-rendered frame. Fast — no DEM download."""
    global latest_event
    try:
        latest_event = load_precomputed(frame_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No precomputed frame: {frame_id}")
    return {"status": "ok", "frame_id": frame_id}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
