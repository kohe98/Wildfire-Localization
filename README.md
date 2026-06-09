# Wildfire Localization

Given a camera image of wildfire smoke and the camera's position and orientation, this tool helps determine the **GPS coordinates of the fire** through an interactive terrain-alignment workflow.

The system renders what the terrain should look like from the camera's perspective using public elevation data, lets the user visually align that projection with the real photo, then ray-casts through the smoke to find where it intersects the ground.

## Installation

### Prerequisites

- **Python 3.10+** and `pip`
- **Node.js 18+** and `npm`

### 1. Clone the repository

```bash
git clone <repository-url>
cd Bicycle
```

### 2. Create a Python virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

### 3. Install frontend dependencies

```bash
cd frontend
npm install
cd ..
```

### 4. Prepare test inputs

The repository includes 22 annotated frames from the Chantry Fire (Arcadia, CA) in `data/`. Run the preparation script to generate the `test_inputs/` directory:

```bash
python3 prepare_test_inputs.py
```

This creates one folder per frame inside `test_inputs/`, each containing an `image.jpg` and a `metadata.json`.

## Usage

You need three terminals: one for the backend, one for the frontend, and one to trigger events.

### 1. Start the backend

```bash
source .venv/bin/activate
cd backend
uvicorn main:app --reload --port 8000
```

The API is now running at `http://localhost:8000`. Verify with `curl http://localhost:8000/healthz`.

### 2. Start the frontend

In a second terminal:

```bash
cd frontend
npm run dev
```

Open `http://localhost:3000` in your browser. You should see a waiting screen.

### 3. Trigger a fire event

In a third terminal:

```bash
source .venv/bin/activate
python trigger.py frame_0014
```

This POSTs a camera image and metadata to the backend. The backend downloads elevation data (first run takes 10-20 seconds), renders a terrain projection, and stores both images. The frontend automatically picks up the new event.

Available test frames: `frame_0014`, `frame_0043`, `frame_0052`, `frame_0238`, `frame_0239`, `frame_0240`, `frame_0244`, `frame_0307`, `frame_0313`, `frame_0323`, `frame_0325`, `frame_0340`, `frame_0341`, `frame_0345`, `frame_0351`, `frame_0352`, `frame_0354`, `frame_0439`, `frame_0442`, `frame_0443`.

### 4. Align and localize

1. The browser shows the camera image with the terrain projection overlaid.
2. **Drag** the projection until terrain features (ridgelines, valleys) match the photo.
3. Use the **Scale** and **Rotation** sliders to fine-tune the fit.
4. Click **Mark Smoke**, then click on the smoke in the image.
5. Click **Calculate Location**. The backend ray-casts through the marked pixel and returns GPS coordinates as well as a brids-eye view of the terrain. If ground-truth data is available, the error distance is shown.

## Implementation Details

### Architecture

```
trigger.py --POST--> Backend (FastAPI, port 8000) --renders--> terrain projection
                          ^                                          |
                          | GET /api/event/latest                    |
                          |                                          v
                     Frontend (React/Vite, port 3000) <-- serves images
                          |
                          | POST /api/localize (after user aligns)
                          v
                     Backend --ray cast--> GPS coordinates
```

The alignment phase runs entirely in the browser at 60fps via CSS transforms. The backend is only called twice: once to fetch the event data and once to compute the final coordinates.

### Repository layout

| Path | Description |
|------|-------------|
| `backend/main.py` | FastAPI server: event ingestion, DEM fetching, projection rendering, ray-cast localization |
| `backend/render/dem_camera_view.py` | Camera model, DEM loaders, z-buffered quad rasteriser with hillshading |
| `backend/render/render_real_dem.py` | Standalone script to batch-render projections from CSV |
| `frontend/src/App.jsx` | React UI: waiting screen, alignment interface, results display |
| `trigger.py` | Simulates the external detection pipeline by POSTing an event to the backend |
| `prepare_test_inputs.py` | Generates `test_inputs/` from `data/camera_parameters.csv` |
| `data/` | Source frames and camera parameters (Chantry Fire, Arcadia CA) |

### API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/event` | Receive a camera image + metadata, render terrain projection |
| `POST` | `/api/event/preset/{frame_id}` | Switch to a pre-rendered frame instantly (no DEM download) |
| `GET` | `/api/event/latest` | Return the most recent event (camera image URL, projection URL, metadata) |
| `POST` | `/api/localize` | Given the user's alignment transform and a smoke pixel, ray-cast to GPS coordinates |
| `GET` | `/healthz` | Health check |

### How the projection works

1. A DEM (Digital Elevation Model) is downloaded from USGS 3DEP via `py3dep` at 10 m resolution.
2. Every DEM cell is projected through a pinhole camera model using the camera's GPS position, elevation, pan, tilt, and zoom.
3. Projected cells are rasterised as quadrilaterals with a z-buffer so that nearer terrain correctly occludes farther terrain.
4. Each cell is coloured using hillshaded terrain colours for visual clarity.

### How alignment correction works

The user corrects errors in the camera parameters by applying a **2D similarity transform** (translate, rotate, scale) to the rendered projection. This 4-DOF transform approximates corrections to pan, tilt, zoom, and roll without re-rendering:

| User control | Corrects |
|---|---|
| Drag horizontally | Pan error |
| Drag vertically | Tilt error |
| Scale | Zoom / FOV error |
| Rotate | Camera roll |

When the user clicks "Calculate", the backend inverts this transform to map the marked smoke pixel back into projection space, then casts a ray from the camera through that pixel to find terrain intersections.

### Camera specifications

The test data comes from an **AXIS Q6135-LE** PTZ camera (part of the [ALERTCalifornia](https://cameras.alertcalifornia.org/) network).

| Spec | Value |
|---|---|
| Resolution | 1920 x 1080 |
| Horizontal FOV at 1x zoom | 58.3 degrees |
| Optical zoom range | 32x |

### Known limitations

- **Single-camera occlusion**: ray casting returns the first terrain intersection. If the fire burns behind a ridge, the reported coordinates will be on the ridge, not behind it. The system reports all intersections so the user can select the correct one.
- **Close-range parallax**: the 2D alignment model assumes the camera position is exact. For fires very close to the camera (< 500 m), small GPS errors in the camera position cause noticeable alignment drift.
- **USA only**: the USGS 3DEP elevation source only covers the United States. For other regions, switch to OpenTopography (requires a free API key) or provide a local GeoTIFF.

## Deployment

### CI/CD pipeline

Pushing to `main` triggers a three-stage GitLab CI pipeline:

1. **prepare** — derives the release name and ingress URLs from the project name and `BASE_URL` CI variable.
2. **build** — Buildah builds and pushes Docker images for `frontend` and `backend` to the GitLab container registry.
3. **deploy** — Helm upgrades the release on the Kubernetes cluster using `helm/values.yaml`.

URLs once deployed:
- Frontend: `https://<project-name>.<BASE_URL>`
- Backend: `https://be.<project-name>.<BASE_URL>`

### Triggering frames on the deployed app

Three frames (`frame_0014`, `frame_0043`, `frame_0052`) are **pre-rendered and baked into the Docker image** (`backend/precomputed/`). The backend loads `frame_0014` automatically on startup, so the app is immediately usable without any trigger.

To switch to a different pre-rendered frame from your local machine:

```bash
uv run trigger.py frame_0043 --url https://be.<project-name>.<BASE_URL>
uv run trigger.py frame_0052 --url https://be.<project-name>.<BASE_URL>
uv run trigger.py frame_0014 --url https://be.<project-name>.<BASE_URL>
```

These calls are instant — they hit `POST /api/event/preset/{frame_id}` which loads the pre-rendered data from disk with no DEM download.

For any other frame (e.g. `frame_0238`), `trigger.py` falls back to the full processing pipeline (`POST /api/event`), which downloads elevation data and renders a projection on the fly. This requires the backend pod to have internet access and takes 10–30 seconds on first run.

### Why pre-rendered frames?

The backend stores the current event in memory. In a Kubernetes deployment with multiple pod replicas, a trigger request and a subsequent browser poll can land on different pods, causing the browser to see "No event received yet" even after a successful trigger. Baking the three demo frames into the image means all pods start with identical state, so the app works correctly regardless of how many replicas are running or which pod handles each request.

### Adding more pre-rendered frames

1. Start the local backend and run `uv run trigger.py <frame_id>` to process the frame.
2. Run the export script to capture the output:
   ```bash
   uv run python -c "
   import json, requests, shutil, numpy as np
   from pathlib import Path
   ev = requests.get('http://localhost:8000/api/event/latest').json()
   frame = '<frame_id>'
   out = Path('backend/precomputed') / frame
   out.mkdir(exist_ok=True)
   shutil.copy(f'test_inputs/{frame}/image.jpg', out / 'camera_image.jpg')
   proj = ev['projection_image_url'].split('/')[-1]
   shutil.copy(f'backend/uploads/{proj}', out / 'projection.jpg')
   ev_slim = {k: v for k, v in ev.items() if k != 'elevation_grid'}
   ev_slim['camera_image_url'] = f'/precomputed/{frame}/camera_image.jpg'
   ev_slim['projection_image_url'] = f'/precomputed/{frame}/projection.jpg'
   (out / 'event.json').write_text(json.dumps(ev_slim, indent=2))
   print('saved', out)
   "
   ```
3. Add the new frame name to `PRECOMPUTED_FRAMES` in `trigger.py`.
4. Commit and push — the frame is baked into the next Docker build.
