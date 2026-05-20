import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, cast

import requests
from flask import Flask, Response, jsonify, render_template_string
app = Flask(__name__)
http = requests.Session()
RECORDINGS_DIR = Path(os.getenv("LUCIUS_RECORDINGS_DIR", "recordings"))

# In-memory recording state keyed by camera id.
recording_jobs: Dict[str, Dict[str, Any]] = {}
recording_lock = threading.Lock()
cameras_lock = threading.RLock()

DISCOVERY_ENABLED = os.getenv("LUCIUS_DISCOVERY", "1") == "1"
DISCOVERY_INTERVAL_SEC = max(5, int(os.getenv("LUCIUS_DISCOVERY_INTERVAL", "20")))
DISCOVERY_SCAN_TIMEOUT = float(os.getenv("LUCIUS_DISCOVERY_TIMEOUT", "0.8"))
DISCOVERY_CONTROL_PORT = int(os.getenv("LUCIUS_DISCOVERY_PORT", "80"))
DISCOVERY_TELNET_PORT = int(os.getenv("LUCIUS_TELNET_PORT", "23"))
DISCOVERY_TELNET_TIMEOUT = float(os.getenv("LUCIUS_TELNET_TIMEOUT", "1.0"))
DISCOVERY_MAX_WORKERS = max(8, int(os.getenv("LUCIUS_DISCOVERY_WORKERS", "48")))

discovered_hosts: Set[str] = set()
discovery_state: Dict[str, Any] = {
    "running": False,
    "last_scan_started": 0.0,
    "last_scan_finished": 0.0,
    "last_scan_found": 0,
    "last_error": "",
}
discovery_thread: Optional[threading.Thread] = None

# Camera health tracking: {camera_id: {"consecutive_failures": int, "last_error": str, "status": str}}
# status: "online" | "offline" | "unknown"
camera_health: Dict[str, Dict[str, Any]] = {}
OFFLINE_THRESHOLD = 3  # Mark offline after N consecutive failures


def _default_cameras() -> Dict[str, Dict[str, object]]:
    default_host = os.getenv("BATCAM_HOST", "").strip()
    if not default_host:
        default_host = "192.168.68.63"
    return {
        "batcam-1": {
            "id": "batcam-1",
            "name": "BatCam Alpha",
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
        with cameras_lock:
                return CAMERAS.get(camera_id)
            


def _snapshot_cameras() -> List[Dict[str, object]]:
    with cameras_lock:
        return [dict(cam) for cam in CAMERAS.values()]


def _camera_exists_for_host(host: str) -> bool:
    with cameras_lock:
        for cam in CAMERAS.values():
            if str(cam.get("host")) == host:
                return True
    return False


def _register_discovered_camera(host: str, control_port: int = 80, stream_port: int = 8000) -> str:
    cam_id: str
    is_new = False
    with cameras_lock:
        for existing in CAMERAS.values():
            if str(existing.get("host")) == host:
                cam_id = str(existing["id"])
                break
        else:
            index = 1
            while f"batcam-{index}" in CAMERAS:
                index += 1
            cam_id = f"batcam-{index}"
            CAMERAS[cam_id] = {
                "id": cam_id,
                "name": f"BatCam {index}",
                "host": host,
                "control_port": int(control_port),
                "stream_port": int(stream_port),
            }
            is_new = True

    _mark_camera_online(cam_id)
    if is_new:
        app.logger.info("Discovered BatCam %s at %s:%d", cam_id, host, control_port)
    return cam_id


def _set_camera_stream_port(camera_id: str, stream_port: int) -> None:
    with cameras_lock:
        cam = CAMERAS.get(camera_id)
        if cam:
            cam["stream_port"] = int(stream_port)


def _mark_camera_online(camera_id: str) -> None:
    """Mark camera as online and reset consecutive failure count."""
    with cameras_lock:
        if camera_id not in camera_health:
            camera_health[camera_id] = {"consecutive_failures": 0, "last_error": "", "status": "online"}
        else:
            camera_health[camera_id]["consecutive_failures"] = 0
            camera_health[camera_id]["last_error"] = ""
            camera_health[camera_id]["status"] = "online"


def _mark_camera_offline(camera_id: str, error: str = "") -> None:
    """Increment failure count and mark offline if threshold exceeded."""
    with cameras_lock:
        if camera_id not in camera_health:
            camera_health[camera_id] = {"consecutive_failures": 1, "last_error": error, "status": "offline"}
        else:
            health = camera_health[camera_id]
            health["consecutive_failures"] += 1
            health["last_error"] = error
            if health["consecutive_failures"] >= OFFLINE_THRESHOLD:
                health["status"] = "offline"
            else:
                health["status"] = "checking"


def _get_camera_health(camera_id: str) -> Dict[str, Any]:
    """Get current health status for a camera."""
    with cameras_lock:
        return camera_health.get(camera_id, {"consecutive_failures": 0, "last_error": "", "status": "unknown"})





def _looks_like_batcam_status(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(k in payload for k in ("volts", "temp", "fan", "light", "armed"))


def _probe_host_for_batcam(host: str) -> Optional[Tuple[str, int, int]]:
    if _camera_exists_for_host(host):
        return None

    url = f"http://{host}:{DISCOVERY_CONTROL_PORT}/status"
    try:
        res = http.get(url, timeout=DISCOVERY_SCAN_TIMEOUT)
        if res.status_code != 200:
            return None
        payload = res.json()
        if not _looks_like_batcam_status(payload):
            return None
        return host, DISCOVERY_CONTROL_PORT, 8000
    except (requests.RequestException, ValueError):
        return None


def _read_telnet_snapshot(host: str, port: int = DISCOVERY_TELNET_PORT) -> str:
    chunks: List[bytes] = []
    try:
        with socket.create_connection((host, port), timeout=DISCOVERY_TELNET_TIMEOUT) as conn:
            conn.settimeout(DISCOVERY_TELNET_TIMEOUT)
            while True:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
    except OSError as exc:
        return f"Telnet snapshot failed: {exc}"

    if not chunks:
        return ""

    return b"".join(chunks).decode("utf-8", errors="replace").strip()


def _local_ipv4() -> Optional[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _discovery_subnets() -> List[IPv4Network]:
    subnets: List[IPv4Network] = []

    raw = os.getenv("LUCIUS_DISCOVERY_CIDR", "").strip()
    if raw:
        for token in [t.strip() for t in raw.split(",") if t.strip()]:
            try:
                net = ip_network(token, strict=False)
                if isinstance(net, IPv4Network):
                    subnets.append(net)
            except ValueError:
                continue

    ip = _local_ipv4()
    if ip:
        try:
            net = ip_network(f"{ip}/24", strict=False)
            if isinstance(net, IPv4Network):
                subnets.append(net)
        except ValueError:
            pass

    # Also scan the /24 of already configured camera hosts.
    for cam in _snapshot_cameras():
        host = str(cam.get("host") or "").strip()
        if not host:
            continue
        try:
            net = ip_network(f"{host}/24", strict=False)
            if isinstance(net, IPv4Network):
                subnets.append(net)
        except ValueError:
            continue

    unique: List[IPv4Network] = []
    seen = set()
    for subnet in subnets:
        key = str(subnet)
        if key in seen:
            continue
        seen.add(key)
        unique.append(subnet)

    return unique


def _discovery_scan_once() -> int:
    subnets = _discovery_subnets()
    if not subnets:
        return 0

    candidates: List[str] = []
    for subnet in subnets:
        for host in subnet.hosts():
            candidates.append(str(host))

    if not candidates:
        return 0

    found = 0
    with ThreadPoolExecutor(max_workers=DISCOVERY_MAX_WORKERS) as executor:
        futures = [executor.submit(_probe_host_for_batcam, host) for host in candidates]
        for future in as_completed(futures):
            result = future.result()
            if not result:
                continue
            host, cport, sport = result
            if host in discovered_hosts:
                continue
            cam_id = _register_discovered_camera(host, cport, sport)
            discovered_hosts.add(host)
            found += 1

    return found


def _discovery_worker() -> None:
    discovery_state["running"] = True
    while True:
        discovery_state["last_scan_started"] = time.time()
        try:
            found = _discovery_scan_once()
            discovery_state["last_scan_found"] = found
            discovery_state["last_error"] = ""
        except Exception as exc:  # pragma: no cover - defensive for daemon loop
            discovery_state["last_error"] = str(exc)
            app.logger.exception("Discovery scan failed")
        finally:
            discovery_state["last_scan_finished"] = time.time()
        time.sleep(DISCOVERY_INTERVAL_SEC)


def start_discovery() -> None:
    global discovery_thread
    if not DISCOVERY_ENABLED:
        return
    if discovery_thread and discovery_thread.is_alive():
        return
    discovery_thread = threading.Thread(target=_discovery_worker, daemon=True, name="lucius-discovery")
    discovery_thread.start()


# Start discovery for both direct execution and WSGI import-based serving.
start_discovery()


def _record_stream_worker(camera_id: str, cam: Dict[str, object], output_path: Path, stop_event: threading.Event):
    stream_url = build_url(cam, "/stream", stream=True)
    req = None
    try:
        req = requests.get(stream_url, stream=True, timeout=(3, 20))
        req.raise_for_status()
        with output_path.open("wb") as f:
            for chunk in req.iter_content(chunk_size=4096):
                if stop_event.is_set():
                    break
                if chunk:
                    f.write(chunk)
    except requests.RequestException:
        pass
    finally:
        if req is not None:
            req.close()
        with recording_lock:
            job = recording_jobs.get(camera_id)
            if job and job.get("stop") is stop_event:
                recording_jobs.pop(camera_id, None)


def _recording_status(camera_id: str) -> Dict[str, object]:
    with recording_lock:
        job = recording_jobs.get(camera_id)
        if not job:
            return {"recording": False}

        started_at = float(cast(float, job["started_at"]))
        output_file = str(job["file"])
        return {
            "recording": True,
            "started_at": started_at,
            "duration_sec": round(max(0.0, time.time() - started_at), 1),
            "file": output_file,
        }


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
        .status-ok { color: #59d4a7; font-weight: 700; }
        .status-warning { color: #f5a623; font-weight: 700; }
        .status-error { color: #ff7272; font-weight: 700; }
        .status-unknown { color: var(--muted); font-weight: 700; }
        .dialog-backdrop {
            position: fixed;
            inset: 0;
            background: rgba(5, 10, 18, 0.72);
            display: none;
            align-items: center;
            justify-content: center;
            padding: 18px;
            z-index: 40;
        }
        .dialog-backdrop.open { display: flex; }
        .dialog {
            width: min(760px, 100%);
            max-height: min(88vh, 860px);
            overflow: auto;
            background: #0f1723;
            border: 1px solid var(--ring);
            border-radius: 18px;
            box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
        }
        .dialog-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            padding: 14px 16px;
            border-bottom: 1px solid var(--ring);
        }
        .dialog-title {
            font-size: 18px;
            font-weight: 800;
        }
        .dialog-subtitle {
            color: var(--muted);
            font-size: 13px;
            margin-top: 2px;
        }
        .dialog-close {
            width: auto;
            padding: 8px 12px;
            background: #23344b;
        }
        .dialog-body {
            display: grid;
            gap: 12px;
            padding: 16px;
        }
        .dialog-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
        }
        .dialog-panel {
            border: 1px solid var(--ring);
            border-radius: 14px;
            padding: 12px;
            background: #111c2b;
        }
        .dialog-panel h3 {
            margin: 0 0 8px;
            font-size: 14px;
            color: var(--text);
        }
        .dialog-serial {
            margin: 0;
            min-height: 220px;
            max-height: 360px;
            overflow: auto;
            white-space: pre-wrap;
            word-break: break-word;
            font-size: 12px;
            line-height: 1.45;
            color: #dbe9ff;
            background: #07101d;
            border: 1px solid var(--ring);
            border-radius: 12px;
            padding: 12px;
        }
        @media (max-width: 760px) {
            .grid { grid-template-columns: 1fr; }
            .meta, .controls { grid-template-columns: 1fr; }
            .dialog-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <main class="wrap">
        <h1>Lucius Hub</h1>
        <p class="subtitle">BatCam Husarnet Mesh Dashboard</p>
        <section id="grid" class="grid"></section>
    </main>
    <div id="serial-dialog" class="dialog-backdrop" role="presentation" aria-hidden="true">
        <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="serial-dialog-title">
            <div class="dialog-head">
                <div>
                    <div id="serial-dialog-title" class="dialog-title">Camera Diagnostics</div>
                    <div id="serial-dialog-subtitle" class="dialog-subtitle">IP, telemetry, and remote boot log</div>
                </div>
                <button class="dialog-close" onclick="closeSerialDialog()">Close</button>
            </div>
            <div class="dialog-body">
                <div class="dialog-grid">
                    <div class="dialog-panel">
                        <h3>Camera</h3>
                        <div class="row"><span>Name</span><strong id="serial-name">--</strong></div>
                        <div class="row"><span>IP</span><strong id="serial-ip">--</strong></div>
                        <div class="row"><span>Status</span><strong id="serial-status">--</strong></div>
                    </div>
                    <div class="dialog-panel">
                        <h3>Telemetry</h3>
                        <div class="row"><span>Battery</span><strong id="serial-volts">--</strong></div>
                        <div class="row"><span>Temp</span><strong id="serial-temp">--</strong></div>
                        <div class="row"><span>Fan / Light</span><strong id="serial-fl">--</strong></div>
                    </div>
                </div>
                <div class="dialog-panel">
                    <h3>Remote Serial</h3>
                    <pre id="serial-log" class="dialog-serial">Open a camera to fetch the latest telnet boot log.</pre>
                </div>
            </div>
        </div>
    </div>
    <script>
        const serialDialogState = {
            cameraId: null,
            camera: null,
            timer: null,
        };
        const cameraIndex = {};

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

        function setDialogTelemetry(payload) {
            const volts = document.getElementById('serial-volts');
            const temp = document.getElementById('serial-temp');
            const fanLight = document.getElementById('serial-fl');
            if (volts) volts.textContent = typeof payload.volts === 'number' ? payload.volts.toFixed(2) + ' V' : '--';
            if (temp) temp.textContent = typeof payload.temp === 'number' ? payload.temp.toFixed(1) + ' C' : '--';
            if (fanLight) {
                const fan = typeof payload.fan === 'number' ? `${payload.fan}%` : '--';
                const light = typeof payload.light === 'boolean' ? (payload.light ? 'ON' : 'OFF') : '--';
                fanLight.textContent = `${fan} / ${light}`;
            }
        }

        function renderEmptyState(root, message) {
            root.innerHTML = `
                <article class="card">
                    <div class="head">
                        <div class="cam-name">No cameras connected</div>
                        <div class="chip">Waiting for discovery</div>
                    </div>
                    <div class="status" style="padding-top: 14px;">
                        ${message}
                    </div>
                </article>
            `;
        }

        function openSerialDialog(camId) {
            const cam = cameraIndex[camId];
            if (!cam) return;
            const backdrop = document.getElementById('serial-dialog');
            const title = document.getElementById('serial-dialog-title');
            const subtitle = document.getElementById('serial-dialog-subtitle');
            const name = document.getElementById('serial-name');
            const ip = document.getElementById('serial-ip');
            const status = document.getElementById('serial-status');
            const log = document.getElementById('serial-log');

            serialDialogState.cameraId = cam.id;
            serialDialogState.camera = cam;
            if (title) title.textContent = `${cam.name} Diagnostics`;
            if (subtitle) subtitle.textContent = `IP ${cam.host} · Telnet port 23`;
            if (name) name.textContent = cam.name;
            if (ip) ip.textContent = cam.host;
            if (status) status.textContent = 'Loading...';
            if (log) log.textContent = 'Fetching remote log...';

            backdrop.classList.add('open');
            backdrop.setAttribute('aria-hidden', 'false');
            refreshSerialDialog(cam.id);
            if (serialDialogState.timer) clearInterval(serialDialogState.timer);
            serialDialogState.timer = setInterval(() => {
                refreshSerialDialog(cam.id);
            }, 2500);
        }

        function closeSerialDialog() {
            const backdrop = document.getElementById('serial-dialog');
            backdrop.classList.remove('open');
            backdrop.setAttribute('aria-hidden', 'true');
            serialDialogState.cameraId = null;
            serialDialogState.camera = null;
            if (serialDialogState.timer) {
                clearInterval(serialDialogState.timer);
                serialDialogState.timer = null;
            }
        }

        async function refreshSerialDialog(camId) {
            if (!camId) return;
            try {
                const [health, statusData, serialData] = await Promise.all([
                    api(`/api/cameras/${camId}/health`),
                    api(`/api/cameras/${camId}/status`),
                    api(`/api/cameras/${camId}/serial`),
                ]);
                const statusNode = document.getElementById('serial-status');
                const logNode = document.getElementById('serial-log');
                const camera = serialDialogState.camera || cameraIndex[camId];
                if (camera) {
                    const ipNode = document.getElementById('serial-ip');
                    const nameNode = document.getElementById('serial-name');
                    if (ipNode) ipNode.textContent = camera.host;
                    if (nameNode) nameNode.textContent = camera.name;
                }
                setDialogTelemetry(statusData.telemetry || {});
                if (statusNode) {
                    const status_badge = health.status || 'unknown';
                    const rec = serialData.recording ? ' · REC' : '';
                    statusNode.textContent = `${status_badge.toUpperCase()}${rec}`;
                }
                if (logNode) {
                    logNode.textContent = serialData.serial || 'No serial output captured yet.';
                }
            } catch (err) {
                const statusNode = document.getElementById('serial-status');
                const logNode = document.getElementById('serial-log');
                if (statusNode) statusNode.textContent = `Error: ${err.message}`;
                if (logNode) logNode.textContent = `Unable to read telnet log: ${err.message}`;
            }
        }

        function refreshSnapshot(camId) {
            const img = document.getElementById(`snap-${camId}`);
            if (!img) return;
            img.src = `/api/cameras/${camId}/snapshot?t=${Date.now()}`;
        }

        async function refreshStatus(camId) {
            try {
                const health = await api(`/api/cameras/${camId}/health`);
                const status_badge = health.status || 'unknown';
                const status_class = {
                    'online': 'status-ok',
                    'offline': 'status-error',
                    'checking': 'status-warning',
                    'unknown': 'status-unknown',
                }[status_badge] || 'status-unknown';
                
                const [data, rec] = await Promise.all([
                    api(`/api/cameras/${camId}/status`),
                    api(`/api/cameras/${camId}/record/status`),
                ]);
                
                setTelemetry(camId, data.telemetry || {});
                renderRecordState(camId, rec);
                const recText = rec.recording ? ` | REC ${Math.floor(rec.duration_sec || 0)}s` : '';
                let status_text = `${status_badge.toUpperCase()}${recText}`;
                if (health.last_error) {
                    status_text += ` (${health.last_error})`;
                }
                setStatus(camId, status_text);
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

        function renderRecordState(camId, payload) {
            const btn = document.getElementById(`record-${camId}`);
            if (!btn) return;
            if (payload && payload.recording) {
                btn.textContent = 'Stop Record';
                btn.dataset.kind = 'danger';
            } else {
                btn.textContent = 'Start Record';
                btn.dataset.kind = 'accent';
            }
        }

        async function toggleRecord(camId) {
            try {
                const current = await api(`/api/cameras/${camId}/record/status`);
                if (current.recording) {
                    setStatus(camId, 'Stopping recording...');
                    const data = await api(`/api/cameras/${camId}/record/stop`, { method: 'POST' });
                    renderRecordState(camId, { recording: false });
                    setStatus(camId, `Recording saved: ${data.file || 'unknown file'}`);
                } else {
                    setStatus(camId, 'Starting recording...');
                    const data = await api(`/api/cameras/${camId}/record/start`, { method: 'POST' });
                    renderRecordState(camId, { recording: true });
                    setStatus(camId, `Recording to ${data.file}`);
                }
            } catch (err) {
                setStatus(camId, `Record failed: ${err.message}`);
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
                <img id="snap-${cam.id}" class="feed" src="/api/cameras/${cam.id}/snapshot" alt="${cam.name} snapshot">
                <div class="meta">
                    <div class="row"><span>Battery</span><strong id="volts-${cam.id}">--</strong></div>
                    <div class="row"><span>Temp</span><strong id="temp-${cam.id}">--</strong></div>
                </div>
                <div class="controls">
                    <button data-kind="accent" onclick="refreshStatus('${cam.id}')">Refresh Status</button>
                    <button data-kind="accent" onclick="openSerialDialog('${cam.id}')">Serial Log</button>
                    <button onclick="sendAction('${cam.id}', 'light')">Toggle Light</button>
                    <button data-kind="danger" onclick="sendAction('${cam.id}', 'night')">Toggle Night</button>
                    <button id="record-${cam.id}" data-kind="accent" onclick="toggleRecord('${cam.id}')">Start Record</button>
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
                if (!data.cameras || data.cameras.length === 0) {
                    renderEmptyState(root, 'No BatCam hosts are configured yet. Set BATCAM_HOST or BATCAMS_JSON, or wait for LAN discovery to find a camera.');
                    return;
                }
                data.cameras.forEach((cam) => {
                    cameraIndex[cam.id] = cam;
                    root.appendChild(buildCard(cam));
                    refreshStatus(cam.id);
                    refreshSnapshot(cam.id);
                    setInterval(() => refreshStatus(cam.id), 5000);
                    setInterval(() => refreshSnapshot(cam.id), 1500);
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
                    for cam in _snapshot_cameras()
        ]
        return jsonify({"cameras": data})


@app.get("/api/cameras/<camera_id>/snapshot")
def camera_snapshot(camera_id: str):
    cam = get_camera_or_none(camera_id)
    if cam is None:
        return jsonify({"error": "camera not found"}), 404

    for path in ("/capture", "/snap"):
        try:
            res = http.get(build_url(cam, path), timeout=3)
            if res.status_code == 200 and res.content:
                _mark_camera_online(camera_id)
                mimetype = res.headers.get("Content-Type", "image/jpeg")
                return Response(
                    res.content,
                    mimetype=mimetype,
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
        except requests.RequestException as e:
            _mark_camera_offline(camera_id, str(e))
            continue

    _mark_camera_offline(camera_id, "snapshot unavailable")
    return jsonify({"error": "snapshot unavailable"}), 503







@app.get("/api/cameras/<camera_id>/status")
def camera_status(camera_id: str):
        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        try:
                res = http.get(build_url(cam, "/status"), timeout=3)
        except requests.RequestException as exc:
                _mark_camera_offline(camera_id, str(exc))
                return jsonify({"error": f"status check failed: {exc}"}), 503

        payload = {}
        try:
                payload = res.json()
        except ValueError:
                payload = {}

        if res.status_code >= 400:
                _mark_camera_offline(camera_id, f"status endpoint returned {res.status_code}")
                return jsonify({"error": "camera status endpoint failed", "http_status": res.status_code}), 502

        _mark_camera_online(camera_id)
        return jsonify({"http_status": res.status_code, "telemetry": payload})


@app.get("/api/cameras/<camera_id>/serial")
def camera_serial(camera_id: str):
        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        serial_text = _read_telnet_snapshot(str(cam["host"]), DISCOVERY_TELNET_PORT)
        return jsonify({
                "http_status": 200,
                "host": cam["host"],
                "telnet_port": DISCOVERY_TELNET_PORT,
                "serial": serial_text,
        })


@app.get("/api/cameras/<camera_id>/health")
def camera_health_status(camera_id: str):
        """Get health/connectivity status for a specific camera."""
        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        health = _get_camera_health(camera_id)
        return jsonify({
                "id": camera_id,
                "status": health.get("status", "unknown"),
                "consecutive_failures": health.get("consecutive_failures", 0),
                "last_error": health.get("last_error", ""),
        })


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


@app.get("/api/cameras/<camera_id>/record/status")
def camera_record_status(camera_id: str):
        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        return jsonify(_recording_status(camera_id))


@app.post("/api/cameras/<camera_id>/record/start")
def camera_record_start(camera_id: str):
        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

        with recording_lock:
                if camera_id in recording_jobs:
                        return jsonify({"error": "recording already active"}), 409

                filename = f"{camera_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mjpeg"
                output_path = RECORDINGS_DIR / filename
                stop_event = threading.Event()
                thread = threading.Thread(
                        target=_record_stream_worker,
                        args=(camera_id, cam, output_path, stop_event),
                        daemon=True,
                )
                recording_jobs[camera_id] = {
                        "thread": thread,
                        "stop": stop_event,
                        "file": str(output_path),
                        "started_at": time.time(),
                }
                thread.start()

        return jsonify({"recording": True, "file": str(output_path)})


@app.post("/api/cameras/<camera_id>/record/stop")
def camera_record_stop(camera_id: str):
        cam = get_camera_or_none(camera_id)
        if cam is None:
                return jsonify({"error": "camera not found"}), 404

        with recording_lock:
                job = recording_jobs.get(camera_id)
                if not job:
                        return jsonify({"error": "recording not active"}), 409
                stop_event = cast(threading.Event, job["stop"])
                thread = cast(threading.Thread, job["thread"])
                out_file = str(job["file"])

        stop_event.set()
        thread.join(timeout=3)

        size = None
        try:
                size = os.path.getsize(out_file)
        except OSError:
                size = None

        return jsonify({"recording": False, "file": out_file, "bytes": size})


@app.get("/health")
def health():
        camera_statuses = {
            cam_id: _get_camera_health(cam_id)
            for cam_id in CAMERAS.keys()
        }
        online_count = sum(1 for h in camera_statuses.values() if h.get("status") == "online")
        return jsonify(
            {
                "ok": True,
                "camera_count": len(_snapshot_cameras()),
                "cameras_online": online_count,
                "camera_statuses": camera_statuses,
                "discovery": {
                    "enabled": DISCOVERY_ENABLED,
                    "running": bool(discovery_state.get("running", False)),
                    "last_scan_started": discovery_state.get("last_scan_started", 0.0),
                    "last_scan_finished": discovery_state.get("last_scan_finished", 0.0),
                    "last_scan_found": discovery_state.get("last_scan_found", 0),
                    "last_error": discovery_state.get("last_error", ""),
                },
            }
        )


if __name__ == "__main__":
    start_discovery()
    app.run(
        host=os.getenv("LUCIUS_HOST", "0.0.0.0"),
        port=int(os.getenv("LUCIUS_PORT", "5000")),
        debug=os.getenv("LUCIUS_DEBUG", "0") == "1",
        threaded=True,
    )