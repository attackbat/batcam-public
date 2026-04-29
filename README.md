# BatCam

An ESP32-S3 (Seeed XIAO with PSRAM) camera node that streams MJPEG over a Husarnet VPN mesh and exposes a local control interface, paired with **Lucius** — a Python/Flask dashboard that aggregates multiple BatCam nodes.

---

## Hardware

| Component | Details |
|---|---|
| MCU | Seeed XIAO ESP32-S3 Sense |
| Camera | OV2640 (on-module) |
| Display board | Waveshare S3 1.47" |
| Storage | optional SD card |
| VPN mesh | Husarnet |

## Features

- MJPEG stream at UXGA-capable OV2640 defaults (runtime stream on port 8000)
- Control page (toggle light, night-vision mode) on port 80
- Temperature-controlled fan via PWM
- Battery voltage telemetry
- Custom setup AP portal for WiFi + Husarnet provisioning (AP: `BATCAM-SETUP`)
- Captive DNS in setup mode (`router.setup` and `192.168.4.1`)
- Husarnet P2P VPN join code stored securely in LittleFS

---

## Firmware Setup

### Prerequisites

- [PlatformIO](https://platformio.org/) (VS Code extension recommended)
- Board: `seeed_xiao_esp32s3`

### Configure setup AP

Before flashing, open `src/main.cpp` and set your own setup AP password:

```cpp
#define SETUP_AP_SSID "BATCAM-SETUP"
#define SETUP_AP_PASS "YOUR_AP_PASSWORD"
```

### Build & Flash

```bash
pio run -e seeed_xiao_esp32s3 -t upload
pio device monitor -b 115200
```

### First boot

1. BatCam will create a WiFi AP named `BATCAM-SETUP`.
2. Connect to it and open `http://router.setup`.
3. If captive DNS does not resolve on your client, use `http://192.168.4.1`.
4. Enter WiFi credentials and optionally a Husarnet join code.
5. Press **Save & Reboot**.
6. BatCam reboots and joins WiFi (and Husarnet if a code is provided).

### Recovery mode

If saved WiFi fails to connect on boot, BatCam automatically re-enters setup AP mode.

---

## Lucius Dashboard

`Lucius.py` is a Flask proxy dashboard that connects to one or more BatCam nodes over Husarnet and provides a unified camera view with controls.

### Prerequisites

```bash
pip install -r requirements.txt
```

### Run — single BatCam

```bash
BATCAM_HOST=batcam-zero python3 Lucius.py
```

### Run — multiple BatCams

```bash
BATCAMS_JSON='[
  {"id":"cam1","name":"Front","host":"batcam-zero","control_port":80,"stream_port":8000},
  {"id":"cam2","name":"Back","host":"batcam-two","control_port":80,"stream_port":8000}
]' python3 Lucius.py
```

Open `http://localhost:5000` in your browser.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `BATCAM_HOST` | `batcam-zero` | Husarnet hostname of a single BatCam |
| `BATCAMS_JSON` | *(unset)* | JSON array for multi-camera setup |
| `LUCIUS_HOST` | `0.0.0.0` | Interface Lucius binds to |
| `LUCIUS_PORT` | `5000` | Port Lucius listens on |
| `LUCIUS_DEBUG` | `0` | Set to `1` to enable Flask debug mode |

---

## API Reference

| Method | Path | Description |
|---|---|---|
| GET | `/api/cameras` | List configured cameras |
| GET | `/api/cameras/<id>/stream` | Proxied MJPEG stream |
| GET | `/api/cameras/<id>/status` | Battery voltage and board temp |
| POST | `/api/cameras/<id>/cmd/light` | Toggle light GPIO |
| POST | `/api/cameras/<id>/cmd/night` | Toggle night-vision mode |
| GET | `/health` | Lucius health check |

---

## License

MIT — see `LICENSE` file.
