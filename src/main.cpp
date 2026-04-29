#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include "esp_camera.h"
#include "esp_http_server.h" 
#include <WiFiManager.h> 
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

const char* HOSTNAME = "batcam-zero";
char husarnet_code[60] = ""; 
bool shouldSaveConfig = false;
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
void saveConfigCallback() {
  Serial.println("[!] Configuration changed, flagging for save.");
  shouldSaveConfig = true;
}

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
  if (LittleFS.begin(true)) {
    if (LittleFS.exists("/config.json")) {
      File configFile = LittleFS.open("/config.json", "r");
      if (configFile) {
        size_t size = configFile.size();
        std::unique_ptr<char[]> buf(new char[size]);
        configFile.readBytes(buf.get(), size);
        StaticJsonDocument<256> doc;
        auto error = deserializeJson(doc, buf.get());
        if (!error) {
          strcpy(husarnet_code, doc["husarnet_code"] | "");
          Serial.println("[+] Loaded Husarnet code from vault.");
        }
      }
    }
  } else {
    Serial.println("[-] Failed to mount FS");
  }
}

void saveCredentials() {
  Serial.println("[+] Saving config to vault...");
  StaticJsonDocument<256> doc;
  doc["husarnet_code"] = husarnet_code;
  File configFile = LittleFS.open("/config.json", "w");
  if (!configFile) {
    Serial.println("[-] Failed to open config file for writing");
    return;
  }
  serializeJson(doc, configFile);
  configFile.close();
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

  WiFiManager wm;
  wm.setSaveConfigCallback(saveConfigCallback);
  
  WiFiManagerParameter custom_husarnet("husarnet", "Husarnet Join Code", husarnet_code, 60);
  wm.addParameter(&custom_husarnet);

  Serial.println("[*] Seeking known networks. Fallback AP: BATCAM-SETUP");
  if (!wm.autoConnect("BATCAM-SETUP", "changeme123")) {
    Serial.println("[-] Failed to connect and hit timeout. Rebooting.");
    ESP.restart();
  }

  if (shouldSaveConfig) {
    strcpy(husarnet_code, custom_husarnet.getValue());
    saveCredentials();
  }

  if (strlen(husarnet_code) > 10) {
    Serial.println("[*] Initiating Husarnet VPN...");
    husarnetClient = husarnet_init();
    husarnet_join(husarnetClient, HOSTNAME, husarnet_code);
  } else {
    Serial.println("[-] No valid Husarnet code found. Operating in local mode only.");
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