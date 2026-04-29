import json
import os
from typing import Dict

import requests
from flask import Flask, Response, jsonify, render_template_string

app = Flask(__name__)
http = requests.Session()


def _default_cameras() -> Dict[str, Dict[str, object]]:
        default_host = os.getenv("BATCAM_HOST", "batcam-zero")
        return {
                "batcam-1": {
                        "id": "batcam-1",
                        "name": "BatCam 1",
                        "host": default_host,
                        "control_port": 80,
                        "stream_port": 8000,
                }
        }


def load_cameras() -> Dict[str, Dict[str, object]]:
        raw = os.getenv("BATCAMS_JSON", "").strip()
        if not raw:
                return _default_cameras()

        try:
                payload = json.loads(raw)
        except json.JSONDecodeError:
                return _default_cameras()

        cameras: Dict[str, Dict[str, object]] = {}
        if isinstance(payload, list):
                for index, item in enumerate(payload, start=1):
                        if not isinstance(item, dict):
                                continue
                        cam_id = str(item.get("id") or f"batcam-{index}")
                        host = str(item.get("host") or "").strip()
                        if not host:
                                continue
                        cameras[cam_id] = {
                                "id": cam_id,
                                "name": str(item.get("name") or cam_id),
                                "host": host,
                                "control_port": int(item.get("control_port", 80)),
                                "stream_port": int(item.get("stream_port", 8000)),
                        }

        return cameras or _default_cameras()


CAMERAS = load_cameras()


def build_url(cam: Dict[str, object], path: str, stream: bool = False) -> str:
        port = cam["stream_port"] if stream else cam["control_port"]
        return f"http://{cam['host']}:{port}{path}"


def get_camera_or_none(camera_id: str):
        return CAMERAS.get(camera_id)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lucius Hub | BatCam Mesh Dashboard</title>
    <style>
        :root {
            --bg: #0f1723;
            --panel: #162232;
            --panel2: #1b2d43;
            --text: #dbe9ff;
            --muted: #89a2c4;
            --accent: #59d4a7;
            --danger: #ff7272;
            --ring: #2a4362;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: "Trebuchet MS", "Segoe UI", sans-serif;
            background: radial-gradient(circle at 0% 0%, #24354b, var(--bg) 55%);
            color: var(--text);
        }
        .wrap {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        h1 {
            margin-top: 0;
            margin-bottom: 8px;
            letter-spacing: 0.5px;
        }
        .subtitle {
            margin: 0 0 20px;
            color: var(--muted);
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
            gap: 16px;
        }
        .card {
            background: linear-gradient(165deg, var(--panel), var(--panel2));
            border: 1px solid var(--ring);
            border-radius: 14px;
            overflow: hidden;
            box-shadow: 0 10px 24px rgba(0, 0, 0, 0.25);
        }
        .head {
            padding: 12px 14px;
            border-bottom: 1px solid var(--ring);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
        }
        .cam-name {
            font-size: 18px;
            font-weight: 700;
        }
        .chip {
            font-size: 12px;
            color: var(--muted);
            border: 1px solid var(--ring);
            border-radius: 999px;
            padding: 2px 10px;
        }
        .feed {
            width: 100%;
            display: block;
            background: #07101d;
            min-height: 240px;
            object-fit: cover;
        }
        .meta {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px;
            padding: 12px 14px;
            color: var(--muted);
            font-size: 14px;
        }
        .row {
            display: flex;
            justify-content: space-between;
            border: 1px solid var(--ring);
            border-radius: 10px;
            padding: 7px 9px;
        }
        .controls {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px;
            padding: 0 14px 14px;
        }
        button {
            border: 1px solid var(--ring);
            border-radius: 10px;
            background: #23344b;
            color: var(--text);
            padding: 10px;
            font-weight: 700;
            cursor: pointer;
        }
        button:hover { background: #2a405d; }
        button[data-kind="accent"] {
            background: #1f4f45;
            border-color: #2f6d61;
        }
        button[data-kind="accent"]:hover { background: #286559; }
        button[data-kind="danger"] {
            background: #572f3a;
            border-color: #784653;
        }
        button[data-kind="danger"]:hover { background: #6a3946; }
        .status {
            padding: 0 14px 14px;
            color: var(--muted);
            font-size: 13px;
            min-height: 20px;
        }
        @media (max-width: 760px) {
            .grid { grid-template-columns: 1fr; }
            .meta, .controls { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <main class="wrap">
        <h1>Lucius Hub</h1>
        <p class="subtitle">BatCam Husarnet Mesh Dashboard</p>
        <section id="grid" class="grid"></section>
    </main>
    <script>
        async function api(path, options = {}) {
            const res = await fetch(path, options);
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(data.error || 'request failed');
            }
            return data;
        }

        function setStatus(camId, text) {
            const node = document.getElementById(`status-${camId}`);
            if (node) node.textContent = text;
        }

        function setTelemetry(camId, payload) {
            const volts = document.getElementById(`volts-${camId}`);
            const temp = document.getElementById(`temp-${camId}`);
            if (volts && typeof payload.volts === 'number') volts.textContent = payload.volts.toFixed(2) + ' V';
            if (temp && typeof payload.temp === 'number') temp.textContent = payload.temp.toFixed(1) + ' C';
        }

        async function refreshStatus(camId) {
            setStatus(camId, 'Querying camera status...');
            try {
                const data = await api(`/api/cameras/${camId}/status`);
                setTelemetry(camId, data.telemetry || {});
                setStatus(camId, `Online (${data.http_status})`);
            } catch (err) {
                setStatus(camId, `Offline: ${err.message}`);
            }
        }

        async function sendAction(camId, action) {
            setStatus(camId, `Sending ${action}...`);
            try {
                const data = await api(`/api/cameras/${camId}/cmd/${action}`, { method: 'POST' });
                setTelemetry(camId, data.telemetry || {});
                setStatus(camId, `${action} sent (${data.http_status})`);
            } catch (err) {
                setStatus(camId, `Action failed: ${err.message}`);
            }
        }

        function buildCard(cam) {
            const card = document.createElement('article');
            card.className = 'card';
            card.innerHTML = `
                <div class="head">
                    <div class="cam-name">${cam.name}</div>
                    <div class="chip">${cam.host}</div>
                </div>
                <img class="feed" src="/api/cameras/${cam.id}/stream" alt="${cam.name} stream">
                <div class="meta">
                    <div class="row"><span>Battery</span><strong id="volts-${cam.id}">--</strong></div>
                    <div class="row"><span>Temp</span><strong id="temp-${cam.id}">--</strong></div>
                </div>
                <div class="controls">
                    <button data-kind="accent" onclick="refreshStatus('${cam.id}')">Refresh Status</button>
                    <button onclick="sendAction('${cam.id}', 'light')">Toggle Light</button>
                    <button data-kind="danger" onclick="sendAction('${cam.id}', 'night')">Toggle Night</button>
                </div>
                <div id="status-${cam.id}" class="status">Waiting for first check...</div>
            `;
            return card;
        }

        async function init() {
            try {
                const data = await api('/api/cameras');
                const root = document.getElementById('grid');
                root.innerHTML = '';
                data.cameras.forEach((cam) => {
                    root.appendChild(buildCard(cam));
                    refreshStatus(cam.id);
                    setInterval(() => refreshStatus(cam.id), 5000);
                });
            } catch (err) {
                document.getElementById('grid').innerHTML = `<p>Failed to load camera list: ${err.message}</p>`;
            }
        }

        init();
    </script>
</body>
</html>
"""


@app.route("/")
def dashboard():
        return render_template_string(DASHBOARD_HTML)


@app.get("/api/cameras")
def list_cameras():
        data = [
                {
                        "id": cam["id"],
                        "name": cam["name"],
                        "host": cam["host"],
                        "control_port": cam["control_port"],
                        "stream_port": cam["stream_port"],
                }
                for cam in CAMERAS.values()
        ]
        return jsonify({"cameras": data})


@app.get("/api/cameras/<camera_id>/stream")
def stream(camera_id: str):
        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        stream_url = build_url(cam, "/stream", stream=True)
        try:
                req = http.get(stream_url, stream=True, timeout=(3, 10))
                if req.status_code != 200:
                        return jsonify({"error": f"camera stream returned {req.status_code}"}), 502
        except requests.RequestException as exc:
                return jsonify({"error": f"stream proxy failed: {exc}"}), 503

        def generate():
                try:
                        for chunk in req.iter_content(chunk_size=2048):
                                if chunk:
                                        yield chunk
                finally:
                        req.close()

        return Response(
                generate(),
                content_type=req.headers.get(
                        "Content-Type", "multipart/x-mixed-replace;boundary=123456789000000000000987654321"
                ),
        )


@app.get("/api/cameras/<camera_id>/status")
def camera_status(camera_id: str):
        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        try:
                res = http.get(build_url(cam, "/status"), timeout=3)
        except requests.RequestException as exc:
                return jsonify({"error": f"status check failed: {exc}"}), 503

        payload = {}
        try:
                payload = res.json()
        except ValueError:
                payload = {}

        if res.status_code >= 400:
                return jsonify({"error": "camera status endpoint failed", "http_status": res.status_code}), 502

        return jsonify({"http_status": res.status_code, "telemetry": payload})


@app.post("/api/cameras/<camera_id>/cmd/<action>")
def camera_cmd(camera_id: str, action: str):
        if action not in {"light", "night"}:
                return jsonify({"error": "unsupported action"}), 400

        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        try:
                res = http.get(build_url(cam, f"/cmd?action={action}"), timeout=3)
        except requests.RequestException as exc:
                return jsonify({"error": f"command failed: {exc}"}), 503

        payload = {}
        try:
                payload = res.json()
        except ValueError:
                payload = {}

        if res.status_code >= 400:
                return jsonify({"error": "camera command endpoint failed", "http_status": res.status_code}), 502

        return jsonify({"http_status": res.status_code, "telemetry": payload})


@app.get("/health")
def health():
        return jsonify({"ok": True, "camera_count": len(CAMERAS)})


if __name__ == "__main__":
        app.run(
                host=os.getenv("LUCIUS_HOST", "0.0.0.0"),
                port=int(os.getenv("LUCIUS_PORT", "5000")),
                debug=os.getenv("LUCIUS_DEBUG", "0") == "1",
        )