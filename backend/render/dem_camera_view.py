"""
Render the view a PTZ camera sees of the surrounding terrain (a Digital
Elevation Model), given:

    - camera GPS position (lat, lon)
    - camera elevation above sea level (feet, as in ALERT California overlay)
    - pan  (deg, compass bearing, 0 = N, 90 = E)
    - tilt (deg, negative = looking down)
    - zoom (×, optical zoom multiplier; 1× = wide, 30× = tele)

Pipeline:
    1. Download a DEM tile that covers the area the camera might see.
    2. Build a 3D point cloud from that DEM (lat, lon, elevation in m).
    3. Project the cloud through a pinhole camera at the given pose / zoom.
    4. Rasterise the visible cells with a z-buffer to get a clean image.
"""
from __future__ import annotations
import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import LightSource


# =====================================================================
# Constants
# =====================================================================
EARTH_RADIUS_M       = 6_378_137.0
FT_TO_M              = 0.3048
DEFAULT_WIDE_FOV_DEG = 58.3          # horizontal FOV at 1× zoom (Axis Q-PTZ)
DEFAULT_IMAGE_W      = 1920
DEFAULT_IMAGE_H      = 1080


# =====================================================================
# 1. DEM loading
# =====================================================================
def download_dem_opentopography(
    west: float, south: float, east: float, north: float,
    api_key: str | None = None,
    demtype: str = "SRTMGL1",
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """
    Download a DEM tile from the OpenTopography Global DEM API.

    Free key: https://portal.opentopography.org/myopentopo
    Set as env var OPENTOPO_API_KEY, or pass api_key= explicitly.

    demtype options:
        SRTMGL1   – 30 m, near-global
        SRTMGL3   – 90 m, near-global
        COP30     – Copernicus 30 m
        NASADEM   – 30 m
        USGS10m   – 10 m, USA only
        USGS1m    – 1 m,  USA only (small areas)

    Returns (elev_m, (west, south, east, north)).
    """
    import requests
    import rasterio

    if api_key is None:
        api_key = os.environ.get("OPENTOPO_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OpenTopography needs a free API key. Get one at "
            "https://portal.opentopography.org/myopentopo and set "
            "OPENTOPO_API_KEY in your environment, or pass api_key=..."
        )

    r = requests.get(
        "https://portal.opentopography.org/API/globaldem",
        params=dict(
            demtype=demtype,
            south=south, north=north, west=west, east=east,
            outputFormat="GTiff",
            API_Key=api_key,
        ),
        timeout=180,
    )
    r.raise_for_status()
    with rasterio.MemoryFile(r.content) as mf, mf.open() as ds:
        elev = ds.read(1).astype(np.float32)
        b = ds.bounds
        return elev, (b.left, b.bottom, b.right, b.top)


def download_dem_py3dep(
    west: float, south: float, east: float, north: float,
    resolution_m: int = 30,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """
    USGS 3DEP DEM via py3dep — USA only, no API key needed.

    `pip install py3dep`
    """
    import py3dep
    da = py3dep.get_dem((west, south, east, north), resolution_m)
    da_ll = da.rio.reproject("EPSG:4326")
    elev = da_ll.values.astype(np.float32)
    elev = np.where(np.isnan(elev), np.nanmin(elev), elev)  # reproject leaves NaN corners
    b = da_ll.rio.bounds()                     # (left, bottom, right, top)
    return elev, b


def load_dem_geotiff(path: str | Path):
    """Load a local GeoTIFF DEM (EPSG:4326)."""
    import rasterio
    with rasterio.open(path) as ds:
        b = ds.bounds
        return ds.read(1).astype(np.float32), (b.left, b.bottom, b.right, b.top)


# =====================================================================
# 2. Camera model + 3D projection
# =====================================================================
@dataclass
class Camera:
    """Pinhole camera model that matches the ALERT California PTZ overlay."""
    lat: float
    lon: float
    alt_m: float                                # camera height above sea level
    pan_deg: float                              # compass bearing, 0 = N, 90 = E
    tilt_deg: float                             # -ve = looking down
    zoom: float                                 # optical zoom multiplier
    img_w: int    = DEFAULT_IMAGE_W
    img_h: int    = DEFAULT_IMAGE_H
    wide_fov_deg: float = DEFAULT_WIDE_FOV_DEG
    roll_deg: float = 0.0                       # rotation around optical axis

    @property
    def fov_h_deg(self) -> float:
        half = math.tan(math.radians(self.wide_fov_deg) / 2.0) / max(self.zoom, 1e-6)
        return 2.0 * math.degrees(math.atan(half))

    @property
    def focal_px(self) -> float:
        return (self.img_w / 2.0) / math.tan(math.radians(self.fov_h_deg) / 2.0)

    @property
    def R(self) -> np.ndarray:
        """ENU world → camera frame (X=right, Y=down, Z=forward)."""
        p, t = math.radians(self.pan_deg), math.radians(self.tilt_deg)
        fwd = np.array([math.sin(p) * math.cos(t),
                        math.cos(p) * math.cos(t),
                        math.sin(t)])  # -ve tilt = looking down
        world_up = np.array([0.0, 0.0, 1.0])
        right = np.cross(fwd, world_up)
        nr = np.linalg.norm(right)
        right = right / nr if nr > 1e-9 else np.array([1.0, 0.0, 0.0])
        down = np.cross(fwd, right)
        down /= np.linalg.norm(down)
        R = np.stack([right, down, fwd], axis=0)
        # Apply roll around the optical axis (Z in camera frame)
        if abs(self.roll_deg) > 1e-9:
            r = math.radians(self.roll_deg)
            R_roll = np.array([[ math.cos(r), math.sin(r), 0.0],
                               [-math.sin(r), math.cos(r), 0.0],
                               [         0.0,         0.0, 1.0]])
            R = R_roll @ R
        return R


def geodetic_to_enu(lat, lon, alt_m, lat0, lon0, alt0_m):
    """Geodetic → local East-North-Up frame at (lat0, lon0, alt0)."""
    lat0_rad = math.radians(lat0)
    east  = np.radians(np.asarray(lon) - lon0) * EARTH_RADIUS_M * math.cos(lat0_rad)
    north = np.radians(np.asarray(lat) - lat0) * EARTH_RADIUS_M
    up    = np.asarray(alt_m) - alt0_m
    return east, north, up


def project_dem(cam: Camera,
                dem_elev: np.ndarray,
                dem_bounds: tuple[float, float, float, float]):
    """
    Project every DEM cell through the camera.

    Returns (u, v, depth, in_front) — all arrays shape == dem_elev.shape.
    `u, v` are pixel coords (may fall outside the image).
    `depth` is distance along the camera's forward axis (m).
    """
    h, w = dem_elev.shape
    west, south, east, north = dem_bounds
    lons = np.linspace(west, east, w)
    lats = np.linspace(north, south, h)         # rasters: top row = north
    LON, LAT = np.meshgrid(lons, lats)

    e, n, up = geodetic_to_enu(LAT, LON, dem_elev,
                               cam.lat, cam.lon, cam.alt_m)
    pts = np.stack([e, n, up], axis=-1)
    cam_xyz = pts @ cam.R.T
    Xc, Yc, Zc = cam_xyz[..., 0], cam_xyz[..., 1], cam_xyz[..., 2]

    f, cx, cy = cam.focal_px, cam.img_w / 2.0, cam.img_h / 2.0
    with np.errstate(divide="ignore", invalid="ignore"):
        u = f * Xc / Zc + cx
        v = f * Yc / Zc + cy
    return u, v, Zc, Zc > 1.0


# =====================================================================
# 3. Render the camera's view (z-buffered quad rasteriser)
# =====================================================================
def _hillshade_rgb(dem_elev: np.ndarray,
                   azdeg=315, altdeg=35, vert_exag=2.0):
    """Per-cell RGB combining elevation colormap + hillshade."""
    ls = LightSource(azdeg=azdeg, altdeg=altdeg)
    rgba = ls.shade(dem_elev, cmap=plt.get_cmap("terrain"),
                    blend_mode="soft",
                    vert_exag=vert_exag, dx=1.0, dy=1.0)
    return rgba[..., :3].astype(np.float32)        # drop alpha


def _bilinear_upsample(arr: np.ndarray, factor: int) -> np.ndarray:
    """Bilinear upsample a 2D array by an integer factor (no extra deps)."""
    if factor <= 1:
        return arr
    H, W = arr.shape
    yi = np.linspace(0, H - 1, H * factor)
    xi = np.linspace(0, W - 1, W * factor)
    y0 = np.floor(yi).astype(np.int32); y1 = np.minimum(y0 + 1, H - 1)
    x0 = np.floor(xi).astype(np.int32); x1 = np.minimum(x0 + 1, W - 1)
    fy = (yi - y0)[:, None]; fx = (xi - x0)[None, :]
    return ((1 - fy) * (1 - fx) * arr[np.ix_(y0, x0)] +
            (1 - fy) *      fx  * arr[np.ix_(y0, x1)] +
                 fy  * (1 - fx) * arr[np.ix_(y1, x0)] +
                 fy  *      fx  * arr[np.ix_(y1, x1)]).astype(arr.dtype)


def crop_dem_to_view(
    cam: Camera,
    dem_elev: np.ndarray,
    dem_bounds: tuple[float, float, float, float],
    max_distance_km: float = 30.0,
    margin_deg: float = 2.0,
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """
    Crop the DEM to a rectangular bounding box that covers the camera's
    view cone (pan ± FOV/2) out to `max_distance_km`. Saves a lot of memory
    and time at high zoom, where most of the DEM is outside the FOV.

    `margin_deg` adds padding on each side of the FOV in compass degrees.
    """
    pan = cam.pan_deg
    half_fov = cam.fov_h_deg / 2.0 + margin_deg

    # Sample bearings around the cone and find the lat/lon extent.
    bearings = np.linspace(pan - half_fov, pan + half_fov, 9)
    distances = np.array([0.0, max_distance_km * 1000.0])
    B, D = np.meshgrid(bearings, distances)
    east  = D * np.sin(np.radians(B))
    north = D * np.cos(np.radians(B))

    dlat = north / 111_320.0
    dlon = east  / (111_320.0 * math.cos(math.radians(cam.lat)))
    lats = cam.lat + dlat
    lons = cam.lon + dlon

    west, south, east_b, north_b = dem_bounds
    crop_w = max(float(lons.min()), west)
    crop_e = min(float(lons.max()), east_b)
    crop_s = max(float(lats.min()), south)
    crop_n = min(float(lats.max()), north_b)
    if crop_e <= crop_w or crop_n <= crop_s:
        return dem_elev, dem_bounds                # no overlap → keep all

    h, w = dem_elev.shape
    j0 = max(0, int(math.floor((crop_w - west)  / (east_b - west)  * (w - 1))))
    j1 = min(w, int(math.ceil ((crop_e - west)  / (east_b - west)  * (w - 1))) + 1)
    i0 = max(0, int(math.floor((north_b - crop_n) / (north_b - south) * (h - 1))))
    i1 = min(h, int(math.ceil ((north_b - crop_s) / (north_b - south) * (h - 1))) + 1)
    cropped = dem_elev[i0:i1, j0:j1]
    new_bounds = (
        west + j0 * (east_b - west) / (w - 1),
        north_b - (i1 - 1) * (north_b - south) / (h - 1),
        west + (j1 - 1) * (east_b - west) / (w - 1),
        north_b - i0 * (north_b - south) / (h - 1),
    )
    return cropped, new_bounds


def render_camera_view(
    cam: Camera,
    dem_elev: np.ndarray,
    dem_bounds: tuple[float, float, float, float],
    sky_rgb: tuple[float, float, float] = (0.62, 0.75, 0.84),
    sun_az_deg: float = 315.0,
    sun_alt_deg: float = 35.0,
    dem_upsample: int = 1,
    auto_crop: bool = True,
    max_distance_km: float = 30.0,
):
    """
    Rasterise the camera's view of the DEM into an (img_h, img_w, 3) RGB
    image with a per-pixel z-buffer. Returns (image, depth_buffer).

    Each DEM cell (i, j) is drawn as a quadrilateral connecting its four
    projected corners. Quads are filled with the hillshaded colour of
    cell (i, j). The z-buffer stores the centroid depth of whichever quad
    currently covers each pixel, so when quads overlap (e.g. behind a
    ridge) the nearer one wins.

    `dem_upsample`  : bilinearly upsample the DEM by this factor before
                      rasterising. Useful at high zoom where the native
                      DEM resolution shows blocky pixels otherwise.
    `auto_crop`     : crop the DEM to the camera's view cone before
                      rendering — saves a lot of work at high zoom.
    `max_distance_km`: how far out to keep DEM data when cropping.
    """
    if auto_crop:
        dem_elev, dem_bounds = crop_dem_to_view(
            cam, dem_elev, dem_bounds, max_distance_km=max_distance_km)
    if dem_upsample > 1:
        dem_elev = _bilinear_upsample(dem_elev, dem_upsample)
    h, w = dem_elev.shape
    u, v, Zc, in_front = project_dem(cam, dem_elev, dem_bounds)

    # ---- Per-cell colour (hillshaded terrain) ----------------------- #
    color = _hillshade_rgb(dem_elev,
                           azdeg=sun_az_deg, altdeg=sun_alt_deg)

    # ---- Output buffers --------------------------------------------- #
    img  = np.tile(np.asarray(sky_rgb, dtype=np.float32),
                   (cam.img_h, cam.img_w, 1))
    zbuf = np.full((cam.img_h, cam.img_w), np.inf, dtype=np.float32)

    # ---- Vectorised quad collection --------------------------------- #
    u00, v00, z00 = u[:-1, :-1], v[:-1, :-1], Zc[:-1, :-1]
    u01, v01, z01 = u[:-1,  1:], v[:-1,  1:], Zc[:-1,  1:]
    u10, v10, z10 = u[ 1:, :-1], v[ 1:, :-1], Zc[ 1:, :-1]
    u11, v11, z11 = u[ 1:,  1:], v[ 1:,  1:], Zc[ 1:,  1:]
    cell_color    = color[:-1, :-1]                  # (h-1, w-1, 3)

    valid_front = (in_front[:-1, :-1] & in_front[:-1, 1:] &
                   in_front[1:,  :-1] & in_front[1:,  1:])

    # Drop quads that span huge depth discontinuities — these are slivers
    # behind ridges and would smear across the image.
    z_max = np.maximum.reduce([z00, z01, z10, z11])
    z_min = np.minimum.reduce([z00, z01, z10, z11])
    valid_depth = (z_max / np.maximum(z_min, 1.0)) < 1.6

    # Keep only quads whose bounding box overlaps the image.
    bb_u0 = np.minimum.reduce([u00, u01, u10, u11])
    bb_u1 = np.maximum.reduce([u00, u01, u10, u11])
    bb_v0 = np.minimum.reduce([v00, v01, v10, v11])
    bb_v1 = np.maximum.reduce([v00, v01, v10, v11])
    on_screen = ((bb_u1 >= 0) & (bb_u0 < cam.img_w) &
                 (bb_v1 >= 0) & (bb_v0 < cam.img_h))

    valid = valid_front & valid_depth & on_screen
    ii, jj = np.where(valid)
    if ii.size == 0:
        return img, zbuf

    # Centroid depth per quad (used both for z-buffer and ordering).
    z_centroid = ((z00 + z01 + z10 + z11) / 4.0)[ii, jj]

    # Painter's algorithm: draw far → near. The per-pixel z-test handles
    # any remaining overlap.
    order = np.argsort(-z_centroid)
    ii, jj = ii[order], jj[order]
    z_centroid = z_centroid[order]

    quad_u = np.stack([u00[ii, jj], u01[ii, jj],
                       u11[ii, jj], u10[ii, jj]], axis=-1)
    quad_v = np.stack([v00[ii, jj], v01[ii, jj],
                       v11[ii, jj], v10[ii, jj]], axis=-1)
    quad_c = cell_color[ii, jj]                       # (N, 3)

    H, W = cam.img_h, cam.img_w
    for k in range(ii.size):
        u_min = max(int(math.floor(quad_u[k].min())), 0)
        u_max = min(int(math.ceil (quad_u[k].max())), W - 1)
        v_min = max(int(math.floor(quad_v[k].min())), 0)
        v_max = min(int(math.ceil (quad_v[k].max())), H - 1)
        if u_max < u_min or v_max < v_min:
            continue
        z = z_centroid[k]
        sub = zbuf[v_min:v_max+1, u_min:u_max+1]
        m = z < sub
        if m.any():
            sub[m] = z
            img[v_min:v_max+1, u_min:u_max+1][m] = quad_c[k]

    return img, zbuf


# =====================================================================
# 4. Convenience wrappers
# =====================================================================
def camera_from_csv_row(row, **kwargs) -> Camera:
    """Build a Camera from a row of the ALERT-format CSV."""
    return Camera(
        lat      = float(row["camera_lat"]),
        lon      = float(row["camera_lon"]),
        alt_m    = float(row["elev"]) * FT_TO_M,
        pan_deg  = float(row["x"]),
        # ALERT CSV uses +y = down; renderer now uses -tilt = down, so negate
        tilt_deg = -float(row["y"]),
        zoom     = float(row["z"]),
        **kwargs,
    )


def display_render(img: np.ndarray, cam: Camera, title: str = "",
                   output_path: str | Path | None = None,
                   show_boresight: bool = True):
    """Render the rasterised image with title bar and optional boresight."""
    fig, ax = plt.subplots(figsize=(16, 9), dpi=110)
    ax.imshow(np.clip(img, 0, 1), interpolation="nearest")
    ax.set_xlim(0, cam.img_w); ax.set_ylim(cam.img_h, 0)

    if show_boresight:
        cx, cy = cam.img_w / 2, cam.img_h / 2
        ax.plot(cx, cy, "+", color="red", markersize=20, mew=2.0)

    ax.set_title(title or
                 f"pan={cam.pan_deg:.1f}°  tilt={cam.tilt_deg:.1f}°  "
                 f"zoom={cam.zoom:.1f}×  (FOV ≈ {cam.fov_h_deg:.1f}°)",
                 fontsize=12)
    ax.set_xlabel("image x (px)"); ax.set_ylabel("image y (px)")
    fig.subplots_adjust(left=0.05, right=0.97, top=0.94, bottom=0.07)
    if output_path:
        fig.savefig(output_path, dpi=110, bbox_inches="tight", pad_inches=0.15)
    return fig


def bbox_around(lat: float, lon: float, half_extent_km: float = 12.0):
    """
    Square bounding box `half_extent_km` km from (lat, lon) in each direction.
    Returns (west, south, east, north).
    """
    dlat = half_extent_km * 1000.0 / 111_320.0
    dlon = half_extent_km * 1000.0 / (111_320.0 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)
