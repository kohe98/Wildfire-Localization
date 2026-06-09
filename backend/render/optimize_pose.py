"""
Iterative pose optimization: align a DEM render to a real camera image
by matching edge maps.

Pipeline (per frame):
    1. Render the DEM with the current camera pose.
    2. Extract edges from both the render and the real photo.
    3. Optimise [delta_pan, delta_tilt, delta_roll, delta_fov] to minimise
       the Chamfer distance between edge point sets.
    4. Optionally re-render with the improved pose and repeat.

Usage:
    from optimize_pose import optimize_pose

    optimized_cam, info = optimize_pose(
        cam, real_image, dem_elev, dem_bounds,
        max_iters=3,
    )
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass

import cv2
import numpy as np
from scipy.optimize import minimize
from scipy.spatial import cKDTree

from dem_camera_view import Camera, render_camera_view


# =====================================================================
# 1. Edge extraction
# =====================================================================

def extract_edges(
    image: np.ndarray,
    low: int = 50,
    high: int = 150,
    blur_ksize: int = 5,
) -> np.ndarray:
    """
    Extract a binary edge map from an image.

    Parameters
    ----------
    image : (H, W, 3) float32 [0-1] or uint8 [0-255], or (H, W) grayscale.

    Returns
    -------
    edges : (H, W) uint8 binary edge map (255 = edge).
    """
    if image.dtype == np.float32 or image.dtype == np.float64:
        gray = (np.clip(image, 0, 1) * 255).astype(np.uint8)
    else:
        gray = image.copy()

    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_RGB2GRAY)

    gray = cv2.GaussianBlur(gray, (blur_ksize, blur_ksize), 0)
    return cv2.Canny(gray, low, high)


def edge_points(edge_map: np.ndarray) -> np.ndarray:
    """Return (N, 2) array of (x, y) edge pixel coordinates."""
    ys, xs = np.nonzero(edge_map)
    return np.column_stack([xs, ys]).astype(np.float64)


# =====================================================================
# 2. Chamfer distance
# =====================================================================

def chamfer_distance(pts_a: np.ndarray, pts_b: np.ndarray) -> float:
    """
    Symmetric Chamfer distance between two point sets.
    Returns mean nearest-neighbour distance (A→B + B→A) / 2.

    If either set is empty, returns a large penalty.
    """
    if len(pts_a) == 0 or len(pts_b) == 0:
        return 1e6

    tree_b = cKDTree(pts_b)
    d_ab, _ = tree_b.query(pts_a)

    tree_a = cKDTree(pts_a)
    d_ba, _ = tree_a.query(pts_b)

    return (d_ab.mean() + d_ba.mean()) / 2.0


# =====================================================================
# 3. Sub-sample edges for speed
# =====================================================================

def subsample_points(pts: np.ndarray, max_points: int = 4000) -> np.ndarray:
    """Randomly subsample to at most max_points for faster KD-tree queries."""
    if len(pts) <= max_points:
        return pts
    idx = np.random.default_rng(42).choice(len(pts), max_points, replace=False)
    return pts[idx]


# =====================================================================
# 4. Cost function: render + edge extract + Chamfer
# =====================================================================

def _apply_deltas(cam: Camera, deltas: np.ndarray) -> Camera:
    """Return a new Camera with pose deltas applied.

    deltas = [d_pan, d_tilt, d_roll, d_fov_scale]
    where d_fov_scale is a multiplicative factor on wide_fov_deg (centred at 1.0).
    """
    c = copy.copy(cam)
    c.pan_deg = cam.pan_deg + deltas[0]
    c.tilt_deg = cam.tilt_deg + deltas[1]
    c.roll_deg = cam.roll_deg + deltas[2]
    c.wide_fov_deg = cam.wide_fov_deg * max(deltas[3], 0.5)
    return c


def make_cost_fn(
    cam: Camera,
    real_edge_pts: np.ndarray,
    dem_elev: np.ndarray,
    dem_bounds: tuple[float, float, float, float],
    render_kwargs: dict | None = None,
    edge_kwargs: dict | None = None,
    max_edge_pts: int = 4000,
):
    """
    Return a callable cost(deltas) -> float for scipy.optimize.

    The cost renders the DEM with the adjusted camera, extracts edges,
    and returns the Chamfer distance to the real image's edges.
    """
    _render_kw = dict(dem_upsample=1, max_distance_km=15.0)
    if render_kwargs:
        _render_kw.update(render_kwargs)
    _edge_kw = dict(low=50, high=150)
    if edge_kwargs:
        _edge_kw.update(edge_kwargs)

    real_pts_sub = subsample_points(real_edge_pts, max_edge_pts)

    def cost(deltas: np.ndarray) -> float:
        trial_cam = _apply_deltas(cam, deltas)
        img, _ = render_camera_view(trial_cam, dem_elev, dem_bounds, **_render_kw)
        render_edges = extract_edges(img, **_edge_kw)
        render_pts = edge_points(render_edges)
        render_pts = subsample_points(render_pts, max_edge_pts)
        return chamfer_distance(real_pts_sub, render_pts)

    return cost


# =====================================================================
# 5. Single-step optimisation
# =====================================================================

@dataclass
class OptimResult:
    """Result of a single optimisation step."""
    cam: Camera
    deltas: np.ndarray
    cost: float
    n_real_edges: int
    n_render_edges: int
    converged: bool


def optimize_step(
    cam: Camera,
    real_image: np.ndarray,
    dem_elev: np.ndarray,
    dem_bounds: tuple[float, float, float, float],
    render_kwargs: dict | None = None,
    edge_kwargs: dict | None = None,
    max_edge_pts: int = 4000,
    bounds: tuple | None = None,
) -> OptimResult:
    """
    Run one round of pose optimisation.

    Parameters
    ----------
    cam : Current camera parameters.
    real_image : (H, W, 3) real camera photo (uint8 or float32).
    dem_elev, dem_bounds : DEM data.
    render_kwargs : Extra kwargs for render_camera_view.
    edge_kwargs : Extra kwargs for extract_edges (low, high, blur_ksize).
    bounds : Bounds for [d_pan, d_tilt, d_roll, d_fov_scale].
             Default: pan +-5 deg, tilt +-5 deg, roll +-3 deg, fov 0.8-1.2x.

    Returns
    -------
    OptimResult with the optimised camera and diagnostics.
    """
    _edge_kw = dict(low=50, high=150)
    if edge_kwargs:
        _edge_kw.update(edge_kwargs)

    real_edges = extract_edges(real_image, **_edge_kw)
    real_pts = edge_points(real_edges)

    if len(real_pts) < 20:
        # Not enough edges to optimise — return the camera unchanged.
        return OptimResult(
            cam=cam, deltas=np.array([0.0, 0.0, 0.0, 1.0]),
            cost=float("inf"), n_real_edges=len(real_pts),
            n_render_edges=0, converged=False,
        )

    cost_fn = make_cost_fn(
        cam, real_pts, dem_elev, dem_bounds,
        render_kwargs=render_kwargs,
        edge_kwargs=edge_kwargs,
        max_edge_pts=max_edge_pts,
    )

    if bounds is None:
        bounds = [(-5.0, 5.0), (-5.0, 5.0), (-3.0, 3.0), (0.8, 1.2)]

    x0 = np.array([0.0, 0.0, 0.0, 1.0])
    result = minimize(
        cost_fn, x0,
        method="Nelder-Mead",
        bounds=bounds,
        options=dict(
            maxiter=200,
            xatol=0.02,     # ~0.02 deg precision
            fatol=0.5,      # cost tolerance in pixels
            adaptive=True,
        ),
    )

    best_cam = _apply_deltas(cam, result.x)

    # Count render edges at the optimised pose for diagnostics.
    best_img, _ = render_camera_view(best_cam, dem_elev, dem_bounds,
                                     **(render_kwargs or {}))
    best_render_edges = extract_edges(best_img, **_edge_kw)
    n_render = int(np.count_nonzero(best_render_edges))

    return OptimResult(
        cam=best_cam,
        deltas=result.x,
        cost=result.fun,
        n_real_edges=len(real_pts),
        n_render_edges=n_render,
        converged=bool(result.success),
    )


# =====================================================================
# 6. Iterative optimise-render loop
# =====================================================================

def optimize_pose(
    cam: Camera,
    real_image: np.ndarray,
    dem_elev: np.ndarray,
    dem_bounds: tuple[float, float, float, float],
    max_iters: int = 3,
    convergence_px: float = 1.0,
    render_kwargs: dict | None = None,
    edge_kwargs: dict | None = None,
    max_edge_pts: int = 4000,
    bounds: tuple | None = None,
    verbose: bool = True,
) -> tuple[Camera, list[OptimResult]]:
    """
    Iteratively optimise camera pose to align DEM render with real image.

    Each iteration:
        1. Run optimize_step to find best [d_pan, d_tilt, d_roll, d_fov].
        2. Apply deltas to produce an updated camera.
        3. Check convergence: stop if deltas are tiny or cost stopped improving.
        4. Use the updated camera as the starting point for the next iteration.

    Parameters
    ----------
    cam : Initial camera parameters from CSV.
    real_image : Real camera photo (H, W, 3).
    dem_elev, dem_bounds : DEM data.
    max_iters : Maximum number of optimise-render cycles.
    convergence_px : Stop if cost improvement is less than this (pixels).
    render_kwargs : Extra kwargs for render_camera_view.
    edge_kwargs : Extra kwargs for extract_edges.
    bounds : Bounds per iteration (shrink automatically on later iters).
    verbose : Print progress.

    Returns
    -------
    (optimized_camera, list_of_OptimResults)
    """
    current_cam = copy.copy(cam)
    history: list[OptimResult] = []
    prev_cost = float("inf")

    for i in range(max_iters):
        # Shrink search bounds on later iterations for fine-tuning.
        if bounds is None:
            scale = max(0.25, 1.0 / (i + 1))
            iter_bounds = [
                (-5.0 * scale, 5.0 * scale),
                (-5.0 * scale, 5.0 * scale),
                (-3.0 * scale, 3.0 * scale),
                (1.0 - 0.2 * scale, 1.0 + 0.2 * scale),
            ]
        else:
            iter_bounds = bounds

        step = optimize_step(
            current_cam, real_image, dem_elev, dem_bounds,
            render_kwargs=render_kwargs,
            edge_kwargs=edge_kwargs,
            max_edge_pts=max_edge_pts,
            bounds=iter_bounds,
        )
        history.append(step)

        if verbose:
            print(f"  iter {i+1}/{max_iters}:  cost={step.cost:.2f}px  "
                  f"d_pan={step.deltas[0]:+.3f}  d_tilt={step.deltas[1]:+.3f}  "
                  f"d_roll={step.deltas[2]:+.3f}  d_fov_s={step.deltas[3]:.4f}  "
                  f"edges(real={step.n_real_edges}, render={step.n_render_edges})")

        if step.n_real_edges < 20:
            if verbose:
                print("  -> too few edges in real image, stopping.")
            break

        improvement = prev_cost - step.cost
        if i > 0 and improvement < convergence_px:
            if verbose:
                print(f"  -> converged (improvement {improvement:.2f}px "
                      f"< {convergence_px}px)")
            break

        prev_cost = step.cost
        current_cam = step.cam

    return current_cam, history


# =====================================================================
# 7. Visualisation helpers
# =====================================================================

def overlay_edges(
    image: np.ndarray,
    edge_map: np.ndarray,
    color: tuple[int, int, int] = (0, 255, 0),
    alpha: float = 0.7,
) -> np.ndarray:
    """Draw edges on top of an image for visual inspection."""
    if image.dtype == np.float32 or image.dtype == np.float64:
        vis = (np.clip(image, 0, 1) * 255).astype(np.uint8).copy()
    else:
        vis = image.copy()
    mask = edge_map > 0
    for c in range(3):
        vis[..., c][mask] = np.clip(
            vis[..., c][mask] * (1 - alpha) + color[c] * alpha, 0, 255
        ).astype(np.uint8)
    return vis


def save_comparison(
    real_image: np.ndarray,
    render_image: np.ndarray,
    out_path: str,
    edge_kwargs: dict | None = None,
):
    """Save a side-by-side comparison with edge overlays."""
    from PIL import Image

    _edge_kw = dict(low=50, high=150)
    if edge_kwargs:
        _edge_kw.update(edge_kwargs)

    real_edges = extract_edges(real_image, **_edge_kw)
    render_edges = extract_edges(render_image, **_edge_kw)

    left = overlay_edges(real_image, render_edges, color=(255, 0, 0))   # render edges in red on real
    right = overlay_edges(render_image, real_edges, color=(0, 255, 0))  # real edges in green on render

    left_pil = Image.fromarray(left)
    right_pil = Image.fromarray(right)

    h = max(left_pil.height, right_pil.height)
    lw = int(left_pil.width * h / left_pil.height)
    rw = int(right_pil.width * h / right_pil.height)
    left_pil = left_pil.resize((lw, h))
    right_pil = right_pil.resize((rw, h))

    combined = Image.new("RGB", (lw + rw, h))
    combined.paste(left_pil, (0, 0))
    combined.paste(right_pil, (lw, 0))
    combined.save(out_path, quality=90)
