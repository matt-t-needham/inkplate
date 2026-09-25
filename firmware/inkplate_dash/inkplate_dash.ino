/*
 * inkplate_dash firmware — Inkplate 13SPECTRA poller
 * ==================================================
 *
 * Written BEFORE the device arrived, against the documented API of the
 * Soldered Inkplate Arduino library. It compiles conceptually but has never
 * touched hardware. Lines marked VERIFY-ON-ARRIVAL are the ones most likely
 * to need adjustment once the real board + board package are in hand
 * (see ../../README.md for the arrival-day checklist).
 *
 * What it does, every wake:
 *   1. Increment boot counter (kept in RTC memory across deep sleep)
 *   2. Join Wi-Fi (15s timeout)
 *   3. GET  <SERVER>/api/display/meta          — tiny JSON version check
 *   4. If image_version != version we last drew:
 *        GET <SERVER>/api/display.png          — pre-dithered 6-color frame
 *        draw it, full panel refresh (~20s on Spectra)
 *   5. POST <SERVER>/api/device/checkin        — telemetry (battery, rssi,
 *        boot count, wake reason, duration, error if any)
 *   6. Deep sleep for the sleep_seconds the server returned
 *      (tethered mode: stay awake and poll — used for bring-up/debugging)
 *
 * The server owns ALL content and the cadence. Reflashing should only ever
 * be needed for Wi-Fi credentials or firmware bugs, never for dashboard
 * changes.
 *
 * Libraries (Arduino IDE → Library Manager):
 *   - InkplateLibrary (Soldered)   VERIFY-ON-ARRIVAL: 13SPECTRA support level
 *   - ArduinoJson (Benoit Blanchon)
 *
 * Board: select the Soldered Inkplate 13SPECTRA board in Arduino IDE after
 * installing the Soldered ESP32 board package (Dasduino board manager URL).
 */

// VERIFY-ON-ARRIVAL: the library may require a board define before the
// include (older Inkplates used e.g. ARDUINO_INKPLATECOLOR guards) — the
// board selection in the IDE usually handles this. If compilation fails
// here, check the library's examples/Inkplate13SPECTRA folder for the
// exact include pattern.
#include "Inkplate.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

#include "config.h"  // copy config.h.example -> config.h and fill in

Inkplate display;

// RTC slow memory: survives deep sleep, wiped on power loss / reset button.
RTC_DATA_ATTR int bootCount = 0;
RTC_DATA_ATTR int shownVersion = -1;

// If anything fails, sleep this long and retry rather than draining the
// battery in a hot loop.
static const uint64_t FALLBACK_SLEEP_S = 15 * 60;

static const int WIFI_TIMEOUT_MS = 15000;
static const int HTTP_TIMEOUT_MS = 30000;

String lastError = "";  // reported in the check-in, shown in the control pane

// ── helpers ──────────────────────────────────────────────────────────────────

bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > WIFI_TIMEOUT_MS) {
      lastError = "wifi timeout";
      return false;
    }
    delay(200);
  }
  return true;
}

// GET /api/display/meta → fills version/sleepSeconds/tethered. false on error.
bool fetchMeta(int &version, long &sleepSeconds, bool &tethered) {
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(String(SERVER_URL) + "/api/display/meta");
  int code = http.GET();
  if (code != 200) {
    lastError = "meta http " + String(code);
    http.end();
    return false;
  }
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getString());
  http.end();
  if (err) {
    lastError = String("meta json: ") + err.c_str();
    return false;
  }
  version = doc["image_version"] | -1;
  sleepSeconds = doc["sleep_seconds"] | (long)FALLBACK_SLEEP_S;
  tethered = doc["tethered"] | false;
  return version >= 0;
}

// Download and draw the pre-dithered frame. The server guarantees the PNG
// contains only the six legal Spectra colors, so the library's decoder just
// maps them 1:1 — no on-device dithering wanted.
bool drawFrame() {
  // VERIFY-ON-ARRIVAL: drawImage signature. On current Inkplate boards it is
  //   drawImage(url, x, y, dither, invert)
  // dither=false on purpose — the server already dithered. If the 13SPECTRA
  // examples show a different call (e.g. a dedicated PNG-from-web helper),
  // use that instead.
  if (!display.drawImage(String(SERVER_URL) + "/api/display.png", 0, 0, false, false)) {
    lastError = "drawImage failed";
    return false;
  }
  display.display();  // full refresh — ~20s on Spectra, expect flashing
  return true;
}

void sendCheckin(long durationMs) {
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(String(SERVER_URL) + "/api/device/checkin");
  http.addHeader("Content-Type", "application/json");

  JsonDocument doc;
  // VERIFY-ON-ARRIVAL: readBattery() exists on current Inkplates (returns
  // volts as double). Confirm the 13SPECTRA variant keeps it.
  doc["battery_voltage"] = display.readBattery();
  doc["rssi"] = WiFi.RSSI();
  doc["boot_count"] = bootCount;
  doc["shown_version"] = shownVersion;
  doc["wake_reason"] = (int)esp_sleep_get_wakeup_cause();  // 0=power-on, 4=timer, ...
  doc["duration_ms"] = durationMs;
  doc["fw_version"] = FW_VERSION;
  if (lastError.length()) doc["error"] = lastError;
  else doc["error"] = (const char *)nullptr;

  String body;
  serializeJson(doc, body);
  http.POST(body);  // best-effort; a failed check-in shouldn't block sleep
  http.end();
}

void goToSleep(uint64_t seconds) {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);
  // VERIFY-ON-ARRIVAL: some Inkplate boards want display peripherals powered
  // down explicitly before deep sleep (e.g. display.setPanelDeepSleep or
  // einkOff()) — check the 13SPECTRA low-power example and add it here.
  esp_deep_sleep_start();
}

// ── main ─────────────────────────────────────────────────────────────────────

void setup() {
  unsigned long t0 = millis();
  bootCount++;
  Serial.begin(115200);
  Serial.printf("inkplate_dash %s boot #%d, shown version %d\n",
                FW_VERSION, bootCount, shownVersion);

  display.begin();

  if (!connectWifi()) {
    Serial.println("ERROR: " + lastError);
    goToSleep(FALLBACK_SLEEP_S);
  }
  Serial.printf("wifi ok, rssi %d dBm\n", WiFi.RSSI());

  int version;
  long sleepSeconds;
  bool tethered;

  // Tethered loop and normal path share one iteration body.
  while (true) {
    lastError = "";
    long thisSleep = FALLBACK_SLEEP_S;

    if (fetchMeta(version, sleepSeconds, tethered)) {
      thisSleep = sleepSeconds;
      if (version != shownVersion) {
        Serial.printf("new frame: v%d -> v%d, drawing\n", shownVersion, version);
        if (drawFrame()) {
          shownVersion = version;
        } else {
          Serial.println("ERROR: " + lastError);
        }
      } else {
        Serial.printf("frame v%d unchanged, skipping redraw\n", version);
      }
    } else {
      Serial.println("ERROR: " + lastError);
      tethered = false;  // can't trust a meta we failed to fetch
    }

    sendCheckin(millis() - t0);

    if (!tethered) {
      Serial.printf("sleeping %lds\n", thisSleep);
      Serial.flush();
      goToSleep((uint64_t)thisSleep);  // never returns
    }

    // Tethered: stay awake, poll again after the short interval. Turning
    // tethered off in the control pane exits this loop at the next poll
    // (meta comes back tethered=false → deep sleep above).
    Serial.printf("tethered: polling again in %lds\n", thisSleep);
    delay(thisSleep * 1000);
    t0 = millis();
  }
}

void loop() {}  // never reached — setup() ends in deep sleep or tethered loop
