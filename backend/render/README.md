# DEM camera-view renderer

Renders the view a PTZ camera sees of the surrounding terrain, given the
camera's GPS position, elevation, and pan/tilt/zoom.

## Files

- **`dem_camera_view.py`** — the library: DEM downloaders, pinhole-camera
  model, projection, and a z-buffered quad rasteriser. No external services
  required at import time; you call into it.
- **`render_real_dem.py`** — example script that reads `camera_parameters.csv`,
  downloads (or loads) a DEM that covers the area around the camera, and
  renders every frame in the CSV.
- **`test_render.py`** — same thing, but with a synthetic DEM. Use this to
  verify the install before downloading real DEM data.

## CSV column meanings

| column                 | meaning                                          |
|------------------------|--------------------------------------------------|
| `elev`                 | camera height above sea level, **in feet**       |
| `x`                    | **pan**: compass bearing in degrees (0=N, 90=E)  |
| `y`                    | **tilt**: degrees, positive = looking down       |
| `z`                    | optical **zoom** multiplier (1× = wide, 30× = tele) |
| `camera_lat`,`_lon`    | camera GPS position                              |

The library converts `elev` to meters for you.

## Setup

```bash
pip install numpy pandas matplotlib rasterio requests
```

For the optional USGS-3DEP DEM source: `pip install py3dep`.

## Usage with real DEM data

The simplest path is OpenTopography (worldwide, free, requires an API key):

1. Get a free key at <https://portal.opentopography.org/myopentopo>
2. `export OPENTOPO_API_KEY=...`
3. Edit `render_real_dem.py` if you want to swap DEM type (SRTMGL1 30m,
   COP30, USGS10m, USGS1m, …) or change `HALF_EXTENT_KM`.
4. `python render_real_dem.py`

USGS 3DEP via `py3dep` is keyless but USA-only.

## Camera intrinsics caveat

The wide-angle FOV defaults to **62°** (Axis Q-series PTZs at 1× zoom). If
your camera is a different model, edit `DEFAULT_WIDE_FOV_DEG` in
`dem_camera_view.py` — that single value drives the entire focal-length
scaling for every zoom level.

## Pipeline

1. **Download DEM** (a 2D array of elevations + a (W,S,E,N) bounding box).
2. **Build 3D point cloud** — every DEM cell becomes a point at
   `(lat, lon, elevation)` in geodetic coordinates.
3. **Project through pinhole camera** — convert world points to a local
   East-North-Up frame at the camera, rotate by `(pan, tilt)`, apply the
   focal length implied by the zoom, get pixel coordinates.
4. **Rasterise with z-buffer** — each DEM cell becomes one filled
   quadrilateral (its 4 projected corners), painted with hill-shaded
   terrain colour, depth-tested per pixel so closer cells occlude
   farther ones.

## Notes on quality vs zoom

A 30 m SRTM cell covers a lot of pixels at 20× zoom, so the output will
look blocky if you don't compensate. The `dem_upsample` parameter
bilinearly upsamples the DEM before rendering. `render_real_dem.py`
already scales this with zoom. For very high zoom on USA terrain you can
also switch to USGS10m or USGS1m through OpenTopography for genuinely
finer detail.

The `auto_crop` option (on by default) crops the DEM to the camera's
view cone before rendering — without this, high-zoom renders waste a lot
of memory on terrain outside the FOV.
