#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include "esp_camera.h"
#include "esp_http_server.h"
#include <WiFi.h>
#include <ArduinoJson.h>
#include <husarnet.h>

// ================= HARDWARE DEFINITIONS =================
#define BAT_PIN           1   
#define FAN_PIN           2   
#define LIGHT_PIN         4   

#define FAN_PWM_CHANNEL   4
#define FAN_PWM_FREQ      1000 
#define FAN_PWM_RES       8
#define FAN_MIN_DUTY      160 

// XIAO Sense Camera Pins
#define PWDN_GPIO_NUM     -1
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM     10
#define SIOD_GPIO_NUM     40
#define SIOC_GPIO_NUM     39
#define Y9_GPIO_NUM       48
#define Y8_GPIO_NUM       11
#define Y7_GPIO_NUM       12
#define Y6_GPIO_NUM       14
#define Y5_GPIO_NUM       16
#define Y4_GPIO_NUM       18
#define Y3_GPIO_NUM       17
#define Y2_GPIO_NUM       15
#define VSYNC_GPIO_NUM    38
#define HREF_GPIO_NUM     47
#define PCLK_GPIO_NUM     13

// Setup AP — change SETUP_AP_PASS before flashing if desired
#define SETUP_AP_SSID  "BATCAM-SETUP"
#define SETUP_AP_PASS  "batcam123"

const char* HOSTNAME = "batcam-zero";
char wifi_ssid[64]     = "";
char wifi_pass[64]     = "";
char husarnet_code[80] = "";
HusarnetClient* husarnetClient = NULL;

httpd_handle_t stream_httpd = NULL;
httpd_handle_t camera_httpd = NULL;

// Global State
int fan_percent = 0;
float battery_volts = 0.0;
bool night_mode = false;
bool light_state = false;
unsigned long last_check = 0;

// ================= UTILITIES =================
float getBatteryVoltage() {
    int raw = analogRead(BAT_PIN);
    return (raw / 4095.0) * 3.3 * 2.0; 
}

float getBoardTemp() {
  return temperatureRead();
}

void setFanSpeed(int duty) {
  ledcWrite(FAN_PIN, duty);
  fan_percent = (duty * 100) / 255;
}

void updateHardwareLogic() {
  battery_volts = getBatteryVoltage();
  float temp = getBoardTemp();
  if (temp >= 55.0) {
    if (temp < 70.0) setFanSpeed(FAN_MIN_DUTY); 
    else setFanSpeed(255); 
  } else {
    setFanSpeed(0); 
  }
}

// ================= FILESYSTEM LOGIC =================
void loadCredentials() {
  if (!LittleFS.begin(true)) { Serial.println("[-] Failed to mount FS"); return; }
  if (!LittleFS.exists("/config.json")) return;
  File f = LittleFS.open("/config.json", "r");
  if (!f) return;
  size_t size = f.size();
  std::unique_ptr<char[]> buf(new char[size + 1]);
  f.readBytes(buf.get(), size);
  buf[size] = '\0';
  f.close();
  StaticJsonDocument<512> doc;
  if (!deserializeJson(doc, buf.get())) {
    strlcpy(wifi_ssid,      doc["ssid"]          | "", sizeof(wifi_ssid));
    strlcpy(wifi_pass,      doc["pass"]          | "", sizeof(wifi_pass));
    strlcpy(husarnet_code,  doc["husarnet_code"] | "", sizeof(husarnet_code));
    Serial.println("[+] Credentials loaded from vault.");
  }
}

void saveCredentials() {
  StaticJsonDocument<512> doc;
  doc["ssid"]          = wifi_ssid;
  doc["pass"]          = wifi_pass;
  doc["husarnet_code"] = husarnet_code;
  File f = LittleFS.open("/config.json", "w");
  if (!f) { Serial.println("[-] Failed to open config for write"); return; }
  serializeJson(doc, f);
  f.close();
  Serial.println("[+] Credentials saved.");
}

// ================= SETUP AP PORTAL =================
static void url_decode(char *dst, const char *src, size_t max) {
  size_t i = 0;
  while (*src && i < max - 1) {
    if (*src == '%' && src[1] && src[2]) {
      char hex[3] = { src[1], src[2], '\0' };
      dst[i++] = (char)strtol(hex, nullptr, 16);
      src += 3;
    } else if (*src == '+') {
      dst[i++] = ' '; src++;
    } else {
      dst[i++] = *src++;
    }
  }
  dst[i] = '\0';
}

static const char* SETUP_HTML_HEAD = R"html(<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BatCam Setup</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1723;color:#dbe9ff;font-family:monospace;padding:20px}
.wrap{max-width:480px;margin:auto}
h1{color:#59d4a7;margin-bottom:4px}
.sub{color:#89a2c4;margin-top:0;margin-bottom:20px;font-size:14px}
.card{background:#162232;border:1px solid #2a4362;border-radius:12px;padding:18px;margin-bottom:16px}
.card b{color:#59d4a7}
label{display:block;color:#89a2c4;font-size:13px;margin-top:14px;margin-bottom:5px}
label:first-of-type{margin-top:8px}
input{width:100%;padding:10px 12px;background:#0f1723;border:1px solid #2a4362;border-radius:8px;color:#dbe9ff;font-size:14px}
input:focus{outline:none;border-color:#59d4a7}
.hint{font-size:12px;color:#4a6a8a;margin:5px 0 0}
.chip{display:inline-block;background:#1f4f45;border:1px solid #2f6d61;border-radius:999px;padding:2px 10px;font-size:12px;color:#59d4a7;margin-top:6px}
.btn{width:100%;padding:13px;margin-top:8px;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;letter-spacing:.5px}
.btn-save{background:#1a4f3f;color:#59d4a7;border:1px solid #2f6d61}
.btn-save:hover{background:#225c4a}
.btn-reboot{background:#162232;color:#89a2c4;border:1px solid #2a4362}
.btn-reboot:hover{background:#1e2d3f}
</style>
</head><body><div class="wrap">
<h1>BatCam Setup</h1>
<p class="sub">Connect to WiFi and optionally join the Husarnet VPN mesh.<br>Device reboots after saving.</p>
<form action="/save" method="POST">
<div class="card"><b>WiFi</b>
<label>Network SSID</label>
<input name="ssid" type="text" placeholder="Enter WiFi name" value=")html";

static const char* SETUP_HTML_MID1 = R"html(">
<label>Password</label>
<input name="pass" type="password" placeholder=")html";

static const char* SETUP_HTML_MID2 = R"html(">
<p class="hint">Leave password blank to keep the current one.</p>
</div>
<div class="card"><b>Husarnet VPN</b> <span style="color:#89a2c4;font-size:12px">(optional)</span>
<label>Join Code</label>
<input name="husarnet_code" type="text" placeholder=")html";

static const char* SETUP_HTML_TAIL = R"html(">
<p class="hint">Found at <b>app.husarnet.com</b> &rarr; your network &rarr; Add element. Leave blank to keep current.</p>
</div>
<button class="btn btn-save" type="submit">&#128190; Save &amp; Reboot</button>
</form>
<form action="/reboot" method="POST" style="margin-top:10px">
<button class="btn btn-reboot" type="submit">&#8635; Reboot Only</button>
</form>
</div></body></html>)html";

static esp_err_t setup_index_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  // Part 1 — up to ssid value=""
  httpd_resp_send_chunk(req, SETUP_HTML_HEAD, strlen(SETUP_HTML_HEAD));
  // Inject current SSID (safe: no special HTML chars expected in an SSID, but sanitise anyway)
  char ssid_safe[68] = {0};
  size_t j = 0;
  for (size_t i = 0; wifi_ssid[i] && j < sizeof(ssid_safe) - 1; i++) {
    // Strip quotes so they can't break the HTML attribute
    if (wifi_ssid[i] != '"' && wifi_ssid[i] != '<' && wifi_ssid[i] != '>') ssid_safe[j++] = wifi_ssid[i];
  }
  ssid_safe[j] = '\0';
  httpd_resp_send_chunk(req, ssid_safe, strlen(ssid_safe));
  // Part 2 — up to password placeholder
  httpd_resp_send_chunk(req, SETUP_HTML_MID1, strlen(SETUP_HTML_MID1));
  const char* pass_hint = wifi_pass[0] ? "Enter new password or leave blank to keep current" : "Enter WiFi password";
  httpd_resp_send_chunk(req, pass_hint, strlen(pass_hint));
  // Part 3 — up to husarnet placeholder
  httpd_resp_send_chunk(req, SETUP_HTML_MID2, strlen(SETUP_HTML_MID2));
  const char* hn_hint = husarnet_code[0] ? "Code is set — enter new code to replace" : "Enter Husarnet join code";
  httpd_resp_send_chunk(req, hn_hint, strlen(hn_hint));
  // Part 4 — close
  httpd_resp_send_chunk(req, SETUP_HTML_TAIL, strlen(SETUP_HTML_TAIL));
  httpd_resp_send_chunk(req, nullptr, 0);
  return ESP_OK;
}

static esp_err_t setup_save_handler(httpd_req_t *req) {
  char raw[800] = {0};
  int len = (int)req->content_len;
  if (len <= 0 || len >= (int)sizeof(raw)) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Bad request");
    return ESP_FAIL;
  }
  int received = httpd_req_recv(req, raw, len);
  if (received <= 0) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Read error");
    return ESP_FAIL;
  }
  raw[received] = '\0';

  // Extract URL-encoded fields
  char enc_ssid[128] = {0}, enc_pass[192] = {0}, enc_hn[192] = {0};
  httpd_query_key_value(raw, "ssid",          enc_ssid, sizeof(enc_ssid));
  httpd_query_key_value(raw, "pass",          enc_pass, sizeof(enc_pass));
  httpd_query_key_value(raw, "husarnet_code", enc_hn,   sizeof(enc_hn));

  char dec_ssid[64] = {0}, dec_pass[64] = {0}, dec_hn[80] = {0};
  url_decode(dec_ssid, enc_ssid, sizeof(dec_ssid));
  url_decode(dec_pass, enc_pass, sizeof(dec_pass));
  url_decode(dec_hn,   enc_hn,   sizeof(dec_hn));

  if (strlen(dec_ssid) > 0) strlcpy(wifi_ssid,     dec_ssid, sizeof(wifi_ssid));
  if (strlen(dec_pass) > 0) strlcpy(wifi_pass,     dec_pass, sizeof(wifi_pass));
  if (strlen(dec_hn)   > 0) strlcpy(husarnet_code, dec_hn,   sizeof(husarnet_code));

  saveCredentials();

  static const char* saved_html =
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<style>body{background:#0f1723;color:#59d4a7;font-family:monospace;text-align:center;padding:40px}</style>"
    "</head><body><h2>&#10003; Saved!</h2><p>Rebooting BatCam...</p></body></html>";
  httpd_resp_set_type(req, "text/html");
  httpd_resp_send(req, saved_html, strlen(saved_html));
  delay(1200);
  ESP.restart();
  return ESP_OK;
}

static esp_err_t setup_reboot_handler(httpd_req_t *req) {
  static const char* reboot_html =
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<style>body{background:#0f1723;color:#89a2c4;font-family:monospace;text-align:center;padding:40px}</style>"
    "</head><body><h2>Rebooting...</h2></body></html>";
  httpd_resp_set_type(req, "text/html");
  httpd_resp_send(req, reboot_html, strlen(reboot_html));
  delay(800);
  ESP.restart();
  return ESP_OK;
}

void startSetupMode() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP(SETUP_AP_SSID, SETUP_AP_PASS);
  Serial.printf("[*] Setup AP: %s  Pass: %s\n", SETUP_AP_SSID, SETUP_AP_PASS);
  Serial.println("[*] Connect and open http://192.168.4.1");

  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  httpd_handle_t setup_httpd = NULL;

  httpd_uri_t uri_index  = { .uri = "/",      .method = HTTP_GET,  .handler = setup_index_handler,  .user_ctx = NULL };
  httpd_uri_t uri_save   = { .uri = "/save",  .method = HTTP_POST, .handler = setup_save_handler,   .user_ctx = NULL };
  httpd_uri_t uri_reboot = { .uri = "/reboot",.method = HTTP_POST, .handler = setup_reboot_handler, .user_ctx = NULL };

  if (httpd_start(&setup_httpd, &cfg) == ESP_OK) {
    httpd_register_uri_handler(setup_httpd, &uri_index);
    httpd_register_uri_handler(setup_httpd, &uri_save);
    httpd_register_uri_handler(setup_httpd, &uri_reboot);
  }
  // Block here — the handlers trigger reboot on save
  while (true) { delay(1000); }
}

bool connectToWiFi() {
  if (strlen(wifi_ssid) == 0) return false;
  WiFi.mode(WIFI_STA);
  WiFi.begin(wifi_ssid, wifi_pass);
  Serial.printf("[*] Connecting to %s", wifi_ssid);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500); Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[+] WiFi connected. IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
  }
  Serial.println("[-] WiFi connection failed.");
  return false;
}



// ================= SERVER HANDLERS =================
#define PART_BOUNDARY "123456789000000000000987654321"
static const char* _STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char* _STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char* _STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t * fb = NULL;
  esp_err_t res = ESP_OK;
  size_t _jpg_buf_len = 0;
  uint8_t * _jpg_buf = NULL;
  char * part_buf[64];

  res = httpd_resp_set_type(req, _STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;

  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      res = ESP_FAIL;
    } else {
      if (fb->format != PIXFORMAT_JPEG) {
        bool jpeg_converted = frame2jpg(fb, 80, &_jpg_buf, &_jpg_buf_len);
        esp_camera_fb_return(fb);
        fb = NULL;
        if (!jpeg_converted) res = ESP_FAIL;
      } else {
        _jpg_buf_len = fb->len;
        _jpg_buf = fb->buf;
      }
    }
    if (res == ESP_OK) {
      size_t hlen = snprintf((char *)part_buf, 64, _STREAM_PART, _jpg_buf_len);
      res = httpd_resp_send_chunk(req, (const char *)part_buf, hlen);
    }
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)_jpg_buf, _jpg_buf_len);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, _STREAM_BOUNDARY, strlen(_STREAM_BOUNDARY));
    if (fb) { esp_camera_fb_return(fb); fb = NULL; _jpg_buf = NULL; } 
    else if (_jpg_buf) { free(_jpg_buf); _jpg_buf = NULL; }
    if (res != ESP_OK) break;
  }
  return res;
}

static esp_err_t index_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  const char* html = R"html(
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BatCam v3.1 // HUSARNET</title>
  <style>
    body { background: #111; color: #0f0; font-family: monospace; padding: 10px; }
    .hud-box { border: 1px solid #0f0; padding: 10px; margin-bottom: 10px; }
    img { width: 100%; border: 2px solid #333; }
    button { background: #000; color: #0f0; border: 1px solid #0f0; padding: 10px; width: 48%; }
    button.active { background: #0f0; color: #000; }
  </style>
</head>
<body>
  <h1>🦇 BATCAM v3.1</h1>
  <div class="hud-box"><img id="stream" src=""></div>
  <div class="hud-box">
    VOLTS: <span id="volts">--</span>V | TEMP: <span id="temp">--</span>C
  </div>
  <div class="hud-box">
    <button id="lightBtn" onclick="toggle('light')">LIGHT</button>
    <button id="nightBtn" onclick="toggle('night')">NVG</button>
  </div>
  <script>
    document.getElementById('stream').src = window.location.protocol + "//" + window.location.hostname + ":8000/stream";
    function toggle(cmd) { fetch('/cmd?action=' + cmd).then(r => r.json()).then(update); }
    function update(data) {
       document.getElementById('volts').innerText = data.volts.toFixed(2);
       document.getElementById('temp').innerText = data.temp.toFixed(1);
    }
    setInterval(() => fetch('/status').then(r => r.json()).then(update), 2000);
  </script>
</body>
</html>
  )html";
  return httpd_resp_send(req, html, strlen(html));
}

static esp_err_t cmd_handler(httpd_req_t *req) {
  char buf[32];
  if (httpd_req_get_url_query_str(req, buf, sizeof(buf)) == ESP_OK) {
    char action[16];
    if (httpd_query_key_value(buf, "action", action, sizeof(action)) == ESP_OK) {
      sensor_t * s = esp_camera_sensor_get();
      if (strcmp(action, "light") == 0) {
        light_state = !light_state;
        digitalWrite(LIGHT_PIN, light_state ? HIGH : LOW);
      }
      if (strcmp(action, "night") == 0) {
        night_mode = !night_mode;
        s->set_special_effect(s, night_mode ? 2 : 0); 
        s->set_ae_level(s, night_mode ? 2 : 0);
      }
    }
  }
  char json[128];
  snprintf(json, sizeof(json), "{\"volts\":%.2f,\"temp\":%.1f}", battery_volts, getBoardTemp());
  httpd_resp_set_type(req, "application/json");
  httpd_resp_send(req, json, strlen(json));
  return ESP_OK;
}

void startCameraServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80; 
  httpd_uri_t index_uri = { .uri = "/", .method = HTTP_GET, .handler = index_handler, .user_ctx = NULL };
  httpd_uri_t cmd_uri = { .uri = "/cmd", .method = HTTP_GET, .handler = cmd_handler, .user_ctx = NULL };
  httpd_uri_t status_uri = { .uri = "/status", .method = HTTP_GET, .handler = cmd_handler, .user_ctx = NULL };
  
  httpd_start(&camera_httpd, &config);
  httpd_register_uri_handler(camera_httpd, &index_uri);
  httpd_register_uri_handler(camera_httpd, &cmd_uri);
  httpd_register_uri_handler(camera_httpd, &status_uri);

  config.server_port = 8000; 
  config.ctrl_port = 32769;
  httpd_uri_t stream_uri = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler, .user_ctx = NULL };
  httpd_start(&stream_httpd, &config);
  httpd_register_uri_handler(stream_httpd, &stream_uri);
}

// ================= SETUP =================
void setup() {
  Serial.begin(115200);
  pinMode(BAT_PIN, INPUT);
  pinMode(LIGHT_PIN, OUTPUT);
  
  ledcAttachChannel(FAN_PIN, FAN_PWM_FREQ, FAN_PWM_RES, FAN_PWM_CHANNEL);
  setFanSpeed(0);

  Serial.println("\n[+] BATCAM ZERO BOOT SEQUENCE INITIATED");

  loadCredentials();

  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0; config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM; config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM; config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM; config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM; config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM; config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM; config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM; config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM; config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000; config.frame_size = FRAMESIZE_HD;
  config.pixel_format = PIXFORMAT_JPEG; config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = CAMERA_FB_IN_PSRAM; config.jpeg_quality = 12; config.fb_count = 2;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) Serial.printf("[-] Camera init failed: 0x%x\n", err);

  // If no WiFi credentials are stored, or connection fails → enter setup portal
  if (!connectToWiFi()) {
    Serial.println("[!] No WiFi or connection failed. Starting setup portal.");
    startSetupMode(); // blocks until save+reboot
  }

  if (strlen(husarnet_code) > 10) {
    Serial.println("[*] Initiating Husarnet VPN...");
    husarnetClient = husarnet_init();
    husarnet_join(husarnetClient, HOSTNAME, husarnet_code);
  } else {
    Serial.println("[-] No Husarnet code — local mode only.");
  }

  startCameraServer();
}

void loop() {
  if (millis() - last_check > 2000) {
    last_check = millis();
    updateHardwareLogic();
  }
  delay(10);
}