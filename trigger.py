# /// script
# dependencies = ["requests"]
# ///
"""
Simulate the external pipeline pushing a fire event to the backend.

Usage:
    uv run trigger.py frame_0014
    uv run trigger.py frame_0014 --url http://localhost:8000
"""

import argparse
import json
import sys
from pathlib import Path

import requests

BACKEND_URL = "http://localhost:8000"

PRECOMPUTED_FRAMES = {"frame_0014", "frame_0043", "frame_0052"}


def trigger(frame_name: str, url: str) -> None:
    # Use the fast preset endpoint for pre-rendered frames (no DEM download)
    if frame_name in PRECOMPUTED_FRAMES:
        response = requests.post(f"{url}/api/event/preset/{frame_name}")
        if response.ok:
            print(f"OK ({response.status_code}): {response.json()}")
        else:
            print(f"ERROR ({response.status_code}): {response.text}")
            sys.exit(1)
        return

    event_dir = Path("test_inputs") / frame_name
    image_path = event_dir / "image.jpg"
    metadata_path = event_dir / "metadata.json"

    if not event_dir.exists():
        print(f"ERROR: {event_dir} does not exist")
        sys.exit(1)
    if not image_path.exists():
        print(f"ERROR: {image_path} not found")
        sys.exit(1)
    if not metadata_path.exists():
        print(f"ERROR: {metadata_path} not found")
        sys.exit(1)

    metadata = json.loads(metadata_path.read_text())

    with open(image_path, "rb") as img:
        response = requests.post(
            f"{url}/api/event",
            files={"image": ("image.jpg", img, "image/jpeg")},
            data={"metadata": json.dumps(metadata)},
        )

    if response.ok:
        print(f"OK ({response.status_code}): {response.json()}")
    else:
        print(f"ERROR ({response.status_code}): {response.text}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("frame", help="Frame folder name, e.g. frame_0014")
    parser.add_argument("--url", default=BACKEND_URL, help="Backend base URL")
    args = parser.parse_args()

    trigger(args.frame, args.url)
