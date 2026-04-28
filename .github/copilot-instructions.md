# BatCam AI Coding Agent Instructions

- Purpose: ESP32-S3 (Seeed XIAO + PSRAM) with OV2640 streams MJPEG and serves a minimal control page; optional SD snapshots when recording is toggled. All runtime logic lives in [src/main.cpp](src/main.cpp).
- Hardware map (Waveshare S3 1.47): data Y9..Y2 → GPIO 48,11,12,14,16,18,17,15; VSYNC 38; HREF 47; PCLK 13; SCCB SIOD 40/SIOC 39; XCLK 10; LED 21; battery sense [BAT_PIN=1](src/main.cpp#L31).
- Streaming stack: multipart MJPEG boundary is constant [`123456789000000000000987654321`](src/main.cpp#L33); stream handler at [src/main.cpp#L52-L79](src/main.cpp#L52-L79) converts non-JPEG frames via `frame2jpg` and frees buffers manually; stream server runs on port 81 at `/stream` and is registered in [src/main.cpp#L350-L366](src/main.cpp#L350-L366).
- Main HTTP server (port 80):
	- `/` serves the HTML/JS control page with stream preview and buttons in [src/main.cpp#L83-L206](src/main.cpp#L83-L206).
	- `/control` GET in [src/main.cpp#L220-L265](src/main.cpp#L220-L265) toggles rotation (0-3), recording flag, and quality mode (VGA quality 10 vs QVGA quality 30). Returns JSON state.
	- `/status` GET in [src/main.cpp#L267-L275](src/main.cpp#L267-L275) reports battery voltage and STA IP.
	- `/config` POST in [src/main.cpp#L278-L323](src/main.cpp#L278-L323) parses raw JSON for `ssid`/`password` (manual string search; keep keys exact) and stores them in `current_ssid/pass`.
- WiFi behavior: defaults come from `WIFI_SSID`/`WIFI_PASS` near the top of [src/main.cpp#L8-L15](src/main.cpp#L8-L15); `connectToWiFi()` in [src/main.cpp#L327-L348](src/main.cpp#L327-L348) runs STA join with 40×500ms attempts (~20s) and no TLS; loop watches for credential changes and reconnects at [src/main.cpp#L404-L412](src/main.cpp#L404-L412).
- Camera init: VGA JPEG, quality 12, XCLK 10MHz, `CAMERA_GRAB_WHEN_EMPTY`, PSRAM frame buffers, `fb_count=2` in [src/main.cpp#L383-L398](src/main.cpp#L383-L398); keep PSRAM flags and double buffering for stability.
- Recording: `/control?record=toggle` flips `recording`; loop writes frames to SD if `SD.begin()` succeeds, naming `/img_<count>.jpg` every 500ms in [src/main.cpp#L415-L429](src/main.cpp#L415-L429). No filesystem init elsewhere—SD must be present when recording is on.
- Battery/telemetry: voltage = `(raw/4095)*3.3*2` in [src/main.cpp#L41-L50](src/main.cpp#L41-L50); status endpoint surfaces it.
- Frontend expectations: JS builds stream URL using host and port 81, rotates via CSS, and polls `/status` every 5s; update any new endpoints to keep CORS same-origin.
- Build/flash (PlatformIO): env `seeed_xiao_esp32s3` in [platformio.ini](platformio.ini); build `pio run -e seeed_xiao_esp32s3`, upload `pio run -e seeed_xiao_esp32s3 -t upload` (921600 baud), monitor `pio device monitor -b 115200 -p /dev/ttyACM1`. Keep build flags `BOARD_HAS_PSRAM`, `-mfix-esp32-psram-cache-issue`, and `ARDUINO_USB_CDC_ON_BOOT=1`; monitor rts/dtr set to 0 for stability.
- Conventions/pitfalls: HTTPS is not used (`WiFiClientSecure` absent); credential POST is a naive parser (avoid changing key names/quoting); stream boundary must stay stable for clients; reconnect logic is blocking but bounded; keep rotation/quality state JSON schema intact for the UI.
- File pointers: primary code [src/main.cpp](src/main.cpp); PlatformIO config [platformio.ini](platformio.ini); no other subsystems currently (prior WhiteBat ingest code is not present).
