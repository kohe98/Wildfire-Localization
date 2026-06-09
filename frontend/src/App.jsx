import { useEffect, useRef, useState } from "react";
import "./App.css";

const API_URL = window.location.hostname === "localhost"
  ? "http://localhost:8000"
  : `https://be.${window.location.hostname}`;

// Images are 1920×1080; we display them at half size.
const DISPLAY_W = 960;
const DISPLAY_H = 540;
const DISPLAY_SCALE = 2; // image pixels per display pixel

// ─── Waiting screen ───────────────────────────────────────────────────────────

function WaitingScreen() {
  return (
    <div className="screen waiting-screen">
      <div className="waiting-icon">🔥</div>
      <h1>Waiting for fire event</h1>
      <p>The system will update automatically when smoke is detected.</p>
      <div className="spinner" />
    </div>
  );
}

// ─── Alignment screen ─────────────────────────────────────────────────────────

function HelpGuide({ visible, onClose }) {
  if (!visible) return null;
  return (
    <div className="help-guide">
      <button className="help-close" onClick={onClose} title="Close">&times;</button>
      <h3>How to use</h3>
      <ol>
        <li><strong>Align the overlay.</strong> The coloured terrain projection is shown on top of the camera image. Drag it so that ridgelines and valleys line up with the real photo.</li>
        <li><strong>Fine-tune.</strong> Use the <em>Scale</em> slider if the overlay is too large or small, and <em>Rotation</em> if it is slightly tilted. Adjust <em>Opacity</em> to see through the overlay.</li>
        <li><strong>Mark the smoke.</strong> Click "Mark Smoke", then click on the base of the smoke plume in the image.</li>
        <li><strong>Calculate.</strong> Click "Calculate Location" to get the GPS coordinates of the fire.</li>
      </ol>
    </div>
  );
}

function AlignmentScreen({ event, transform, setTransform, smokePixel, setSmokePixel, onCalculate, loading, error }) {
  const [opacity, setOpacity] = useState(0.55);
  const [animate, setAnimate] = useState(false);
  const [mode, setMode] = useState("drag"); // "drag" | "mark"
  const [showHelp, setShowHelp] = useState(true);
  const dragRef = useRef(null);
  const containerRef = useRef(null);
  const animRef = useRef(null);
  const animDirRef = useRef(-1); // -1 = fading out, 1 = fading in

  useEffect(() => {
    if (!animate) { cancelAnimationFrame(animRef.current); return; }
    let last = null;
    const step = (t) => {
      if (last !== null) {
        const delta = (t - last) / 1000;
        setOpacity(prev => {
          let next = prev + animDirRef.current * 0.5 * delta;
          if (next >= 1) { next = 1; animDirRef.current = -1; }
          if (next <= 0) { next = 0; animDirRef.current = 1; }
          return next;
        });
      }
      last = t;
      animRef.current = requestAnimationFrame(step);
    };
    animRef.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(animRef.current);
  }, [animate]);
  const meta = event.metadata;
  const bbox = meta.smoke_bbox;

  const onProjectionMouseDown = (e) => {
    if (mode !== "drag") return;
    e.preventDefault();
    dragRef.current = { startX: e.clientX, startY: e.clientY, dx: transform.dx, dy: transform.dy };
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
  };

  const onMouseMove = (e) => {
    if (!dragRef.current) return;
    setTransform(t => ({
      ...t,
      dx: dragRef.current.dx + (e.clientX - dragRef.current.startX),
      dy: dragRef.current.dy + (e.clientY - dragRef.current.startY),
    }));
  };

  const onMouseUp = () => {
    dragRef.current = null;
    window.removeEventListener("mousemove", onMouseMove);
    window.removeEventListener("mouseup", onMouseUp);
  };

  const onContainerClick = (e) => {
    if (mode !== "mark") return;
    const rect = containerRef.current.getBoundingClientRect();
    // Convert display coords → image coords (1920×1080)
    setSmokePixel([
      (e.clientX - rect.left) * DISPLAY_SCALE,
      (e.clientY - rect.top) * DISPLAY_SCALE,
    ]);
    setMode("drag");
  };

  const bboxTL = bbox ? [bbox.top_left[0] / DISPLAY_SCALE, bbox.top_left[1] / DISPLAY_SCALE] : null;
  const bboxBR = bbox ? [bbox.bottom_right[0] / DISPLAY_SCALE, bbox.bottom_right[1] / DISPLAY_SCALE] : null;
  const smokeD = smokePixel ? [smokePixel[0] / DISPLAY_SCALE, smokePixel[1] / DISPLAY_SCALE] : null;

  return (
    <div className="screen alignment-screen">
      <div className="alignment-header">
        <h2>
          Align terrain projection
          {!showHelp && <button className="help-toggle" onClick={() => setShowHelp(true)} title="Show help">?</button>}
        </h2>
        <p>Drag the overlay until terrain features match the camera image. Then mark the smoke and calculate.</p>
      </div>

      <HelpGuide visible={showHelp} onClose={() => setShowHelp(false)} />

      <div
        className="image-container"
        ref={containerRef}
        style={{ width: DISPLAY_W, height: DISPLAY_H, cursor: mode === "mark" ? "crosshair" : "default" }}
        onClick={onContainerClick}
      >
        <img className="layer" src={`${API_URL}${event.camera_image_url}`}
          width={DISPLAY_W} height={DISPLAY_H} draggable={false} />

        <img
          className="layer"
          src={`${API_URL}${event.projection_image_url}`}
          width={DISPLAY_W} height={DISPLAY_H}
          draggable={false}
          style={{
            opacity,
            transform: `translate(${transform.dx}px, ${transform.dy}px) rotate(${transform.rotation}deg) scale(${transform.scale})`,
            transformOrigin: "center center",
            cursor: mode === "drag" ? "grab" : "crosshair",
            pointerEvents: mode === "drag" ? "auto" : "none",
          }}
          onMouseDown={onProjectionMouseDown}
        />

        {bboxTL && bboxBR && (
          <div className="smoke-bbox" style={{
            left: bboxTL[0], top: bboxTL[1],
            width: bboxBR[0] - bboxTL[0], height: bboxBR[1] - bboxTL[1],
          }} />
        )}

        {smokeD && (
          <div className="smoke-pin" style={{ left: smokeD[0] - 7, top: smokeD[1] - 7 }} />
        )}
      </div>

      <div className="controls">
        <div className="sliders">
          <label>Opacity
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <input type="range" min="0" max="1" step="0.05" value={opacity}
                onChange={e => { setAnimate(false); setOpacity(+e.target.value); }} />
              <input type="checkbox" checked={animate} onChange={e => setAnimate(e.target.checked)}
                title="Animate opacity" style={{ accentColor: "#f97316", cursor: "pointer", width: 15, height: 15 }} />
            </div>
          </label>
          <label>Scale
            <input type="range" min="0.5" max="6" step="0.02" value={transform.scale}
              onChange={e => setTransform(t => ({ ...t, scale: +e.target.value }))} />
          </label>
          <label>Rotation
            <input type="range" min="-20" max="20" step="0.5" value={transform.rotation}
              onChange={e => setTransform(t => ({ ...t, rotation: +e.target.value }))} />
          </label>
        </div>

        <div className="buttons">
          <button onClick={() => setTransform({ dx: 0, dy: 0, scale: event.render_fov_scale || 3, rotation: 0 })}>Reset</button>
          <button
            className={mode === "mark" ? "active" : ""}
            onClick={() => setMode(m => m === "mark" ? "drag" : "mark")}
          >
            {mode === "mark" ? "Click on smoke..." : "Mark Smoke"}
          </button>
          <button className="primary" onClick={onCalculate} disabled={loading || !smokePixel}>
            {loading ? "Calculating..." : "Calculate Location"}
          </button>
        </div>
      </div>

      {error && <p className="error">{error}</p>}
    </div>
  );
}

// ─── Results screen ───────────────────────────────────────────────────────────

function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6_378_137;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function ResultsScreen({ result, onReset }) {
  const [selected, setSelected] = useState(0);

  return (
    <div className="screen results-screen">
      <h2>Fire Location Candidates</h2>
      <p>
        The ray hit {result.candidates.length} terrain surface{result.candidates.length !== 1 ? "s" : ""}.
        Select the most likely fire location.
      </p>

      <div className="candidates">
        {result.candidates.map((c, i) => (
          <div key={i} className={`candidate ${selected === i ? "selected" : ""}`} onClick={() => setSelected(i)}>
            <div className="candidate-number">{i + 1}</div>
            <div className="candidate-info">
              <div className="candidate-coords">{c.lat.toFixed(5)}°N &nbsp; {Math.abs(c.lon).toFixed(5)}°W</div>
              <div className="candidate-distance">{Math.round(c.distance_m)} m from camera</div>
            </div>
            {selected === i && <div className="candidate-check">✓</div>}
          </div>
        ))}
      </div>

      {result.ground_truth && (
        <div className="ground-truth">
          <strong>Ground truth:</strong>{" "}
          {result.ground_truth.fire_lat.toFixed(5)}°N, {Math.abs(result.ground_truth.fire_lon).toFixed(5)}°W
        </div>
      )}

      <div className="result-output">
        {result.baseline && (
          <div className="result-baseline">
            <strong>Unaligned baseline:</strong>{" "}
            {result.baseline.lat.toFixed(5)}°N, {Math.abs(result.baseline.lon).toFixed(5)}°W
            {result.ground_truth && (() => {
              const err = haversineM(
                result.baseline.lat, result.baseline.lon,
                result.ground_truth.fire_lat, result.ground_truth.fire_lon,
              );
              return <span> · {Math.round(err)} m from ground truth</span>;
            })()}
          </div>
        )}
        <strong>Confirmed:</strong>{" "}
        {result.candidates[selected].lat.toFixed(5)}°N,{" "}
        {Math.abs(result.candidates[selected].lon).toFixed(5)}°W
        <span className="result-distance"> — {Math.round(result.candidates[selected].distance_m)} m from camera</span>
        {result.ground_truth && (() => {
          const err = haversineM(
            result.candidates[selected].lat, result.candidates[selected].lon,
            result.ground_truth.fire_lat, result.ground_truth.fire_lon,
          );
          const baseErr = result.baseline ? haversineM(
            result.baseline.lat, result.baseline.lon,
            result.ground_truth.fire_lat, result.ground_truth.fire_lon,
          ) : null;
          const improvement = baseErr ? Math.round((baseErr - err) / baseErr * 100) : null;
          return <>
            <span className="result-error"> · {Math.round(err)} m from ground truth</span>
            {improvement !== null && (
              <span className={improvement >= 0 ? "result-improvement" : "result-degradation"}>
                {" "}({improvement >= 0 ? "+" : ""}{improvement}% vs baseline)
              </span>
            )}
          </>;
        })()}
      </div>

      {result.overview_image_url && (
        <div className="overview-container">
          <h3>Bird's Eye View</h3>
          <img src={`${API_URL}${result.overview_image_url}`} alt="Bird's eye overview"
            className="overview-image" />
        </div>
      )}

      <button onClick={onReset}>New Event</button>
    </div>
  );
}

// ─── Root ─────────────────────────────────────────────────────────────────────

export default function App() {
  const [phase, setPhase] = useState("waiting");
  const [event, setEvent] = useState(null);
  const [transform, setTransform] = useState({ dx: 0, dy: 0, scale: 3, rotation: 0 });
  const [smokePixel, setSmokePixel] = useState(null);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const lastUrlRef = useRef(null);

  useEffect(() => {
    if (phase !== "waiting") return;
    const poll = async () => {
      try {
        const res = await fetch(`${API_URL}/api/event/latest`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.camera_image_url === lastUrlRef.current) return;
        lastUrlRef.current = data.camera_image_url;
        setEvent(data);
        const bbox = data.metadata?.smoke_bbox;
        setSmokePixel(bbox ? [
          (bbox.top_left[0] + bbox.bottom_right[0]) / 2,
          (bbox.top_left[1] + bbox.bottom_right[1]) / 2,
        ] : null);
        setTransform({ dx: 0, dy: 0, scale: data.render_fov_scale || 3, rotation: 0 });
        setError(null);
        setPhase("aligning");
      } catch (_) {}
    };
    const interval = setInterval(poll, 2000);
    return () => clearInterval(interval);
  }, [phase]);

  const handleCalculate = async () => {
    if (!smokePixel) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/localize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          smoke_pixel: smokePixel,
          dx: transform.dx * DISPLAY_SCALE,
          dy: transform.dy * DISPLAY_SCALE,
          scale: transform.scale,
          rotation_deg: transform.rotation,
        }),
      });
      if (!res.ok) throw new Error((await res.json()).detail ?? "Unknown error");
      setResult(await res.json());
      setPhase("results");
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  if (phase === "waiting") return <WaitingScreen />;
  if (phase === "aligning") return (
    <AlignmentScreen
      event={event}
      transform={transform}
      setTransform={setTransform}
      smokePixel={smokePixel}
      setSmokePixel={setSmokePixel}
      onCalculate={handleCalculate}
      loading={loading}
      error={error}
    />
  );
  return <ResultsScreen result={result} onReset={() => { setPhase("waiting"); setResult(null); }} />;
}
