from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
RENDER_DIR = ROOT / "backend" / "render" / "out"
OUT_DIR = ROOT / "backend" / "render" / "compare"
OUT_DIR.mkdir(parents=True, exist_ok=True)

render_files = {p.stem: p for p in RENDER_DIR.glob("*.png")}
data_files = {p.stem: p for p in DATA_DIR.glob("*.jpg")}

shared = sorted(set(render_files) & set(data_files))
print(f"Found {len(shared)} matching frames")

for stem in shared:
    left = Image.open(data_files[stem]).convert("RGB")
    right = Image.open(render_files[stem]).convert("RGB")

    h = max(left.height, right.height)
    lw = int(left.width * h / left.height)
    rw = int(right.width * h / right.height)
    left = left.resize((lw, h))
    right = right.resize((rw, h))

    combined = Image.new("RGB", (lw + rw, h), (0, 0, 0))
    combined.paste(left, (0, 0))
    combined.paste(right, (lw, 0))
    combined.save(OUT_DIR / f"{stem}.jpg", quality=90)

print(f"Wrote {len(shared)} images to {OUT_DIR}")
