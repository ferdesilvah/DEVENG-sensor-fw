#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <Preferences.h>

// ---------- Pins (GPIO numbers, Seeed XIAO ESP32-C3) ----------
#define PIN_SDA   20   // D7, OLED
#define PIN_SCL   8    // D8, OLED
#define PIN_BTN   6    // D4, other side to GND, changes OLED page
#define PIN_P1    3    // D1, pressure sensor 1 (100 psi)
#define PIN_P2    4    // D2, pressure sensor 2 (20 psi)
#define PIN_FLOW  21   // D6, flow sensor signal (yellow wire)

// ---------- Pressure sensor settings (NOT calibrated) ----------
// Ratiometric sensors: output = 10% to 90% of supply = 0 to full scale.
// Measure the real supply voltage and update SUPPLY_V.
#define P1_FULLSCALE_PSI 100.0f
#define P2_FULLSCALE_PSI 20.0f
#define SUPPLY_V         3.3f
// Sensors are powered from the XIAO 3V3 pin (no dividers). They are rated
// for 5 V; the span below is the datasheet 80%-of-supply scaled to 3.3 V.
// Zero (0 psi) voltage per sensor, measured open to air on 2026-11-13.
// Serial 'z' re-captures zero (open to air) and saves it to flash (NVS),
// which overrides these defaults. Serial 'zc' returns to these defaults.
#define P1_ZERO_V_DEFAULT 0.4606f
#define P2_ZERO_V_DEFAULT 0.4492f
float p1ZeroV = P1_ZERO_V_DEFAULT;
float p2ZeroV = P2_ZERO_V_DEFAULT;
Preferences prefs;

// ---------- Flow calibration (from 13 gravimetric runs, 6.4 to 18.4 Hz) ----------
// pulses per liter = PPL_A * Hz + PPL_B
// L/min = Hz * 60 / pulses per liter
// Below HZ_MIN_VALID the calibration was not tested.
const float PPL_A = 0.31824f;
const float PPL_B = 28.929f;
const float HZ_MIN_VALID = 6.0f;

// Sample interval. Serial 'i <ms>' changes it (100-10000). Shorter intervals
// give coarser flow readings: at 10 Hz flow, 500 ms = ~5 pulses per sample.
uint32_t updateMs = 500;

U8G2_SSD1306_128X32_UNIVISION_F_HW_I2C u8g2(U8G2_R2, U8X8_PIN_NONE);

volatile uint32_t pulseCount = 0;
void IRAM_ATTR onPulse() { pulseCount++; }

// ---------- Test runs ----------
// A run resets total volume and peaks and starts a run timer. Every serial
// line ends with "t <device s>  RUN <n> <run s>" or "t <device s>  IDLE".
// Start/stop: serial 's' / 'x', or hold the button >= 1 s.
// Run numbers are saved in flash so they stay unique across reboots.
bool running = false;
uint32_t runNum = 0;
uint32_t runStartMs = 0;

// Button (polled): short press = next OLED page, hold >= 1 s = start/stop run.
bool btnDown = false, btnLongDone = false;
uint32_t btnDownMs = 0, btnUpMs = 0;

uint32_t lastMs = 0, lastPulses = 0;
float totalL = 0;
float peak1 = -1e9, peak2 = -1e9;   // max psi since boot / 'p' / 'z'
int page = 0;
const int NUM_PAGES = 3;
String inbuf;

float readVolts(int pin) {
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) sum += analogReadMilliVolts(pin);
  return sum / 16.0f / 1000.0f;
}

// Linear: zero voltage -> 0 psi, nominal span 80% of supply = full scale.
// Slope is the datasheet value scaled to SUPPLY_V - unverified at 3.3 V.
float voltsToPsi(float v, float zeroV, float fullScale) {
  return (v - zeroV) / (0.80f * SUPPLY_V) * fullScale;
}

const char* pStatus(float v) {
  if (v < 0.15f) return "NO SIG";
  if (v > 3.05f) return "HIGH";
  return "OK";
}

float hzToLpm(float f) {
  if (f <= 0) return 0;
  return f * 60.0f / (PPL_A * f + PPL_B);
}

void drawLines(const char* l0, const char* l1, const char* l2, const char* l3) {
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawStr(0, 7, l0);
  u8g2.drawStr(0, 15, l1);
  u8g2.drawStr(0, 23, l2);
  u8g2.drawStr(0, 31, l3);
  u8g2.sendBuffer();
}

void startRun() {
  if (running) { Serial.printf("Run %lu already running.\n", (unsigned long)runNum); return; }
  runNum++;
  prefs.putUInt("run", runNum);
  running = true;
  totalL = 0;
  peak1 = peak2 = -1e9;
  lastMs = millis();          // first sample of the run covers a full interval
  lastPulses = pulseCount;
  runStartMs = lastMs;
  Serial.printf("Run %lu started.\n", (unsigned long)runNum);
}

void stopRun() {
  if (!running) { Serial.println("No run in progress."); return; }
  running = false;
  Serial.printf("Run %lu stopped: %.1f s, %.3f L, peak P1 %.2f psi, P2 %.2f psi.\n",
    (unsigned long)runNum, (millis() - runStartMs) / 1000.0f, totalL, peak1, peak2);
}

void pollButton() {
  bool down = digitalRead(PIN_BTN) == LOW;
  uint32_t t = millis();
  if (down && !btnDown && t - btnUpMs > 50) {
    btnDown = true; btnDownMs = t; btnLongDone = false;
  } else if (down && btnDown && !btnLongDone && t - btnDownMs >= 1000) {
    btnLongDone = true;
    if (running) stopRun(); else startRun();
  } else if (!down && btnDown && t - btnDownMs > 50) {
    btnDown = false; btnUpMs = t;
    if (!btnLongDone) page = (page + 1) % NUM_PAGES;
  }
}

void printHelp() {
  Serial.println("Flow + pressure monitor. Button: tap = next page, hold 1 s = start/stop run.");
  Serial.println("Serial: 's' start run, 'x' stop run, 'i <ms>' sample interval, 'r' reset total,");
  Serial.println("        'p' reset peaks, 'z' zero pressure (open to air), 'zc' clear zero, 'h' help.");
  Serial.printf("Pressure zero: P1 %.4f V, P2 %.4f V. Interval %lu ms. Last run #%lu.\n",
    p1ZeroV, p2ZeroV, (unsigned long)updateMs, (unsigned long)runNum);
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);
  delay(1500);

  Wire.setPins(PIN_SDA, PIN_SCL);
  Wire.begin();
  Wire.setClock(400000);
  u8g2.begin();

  pinMode(PIN_BTN, INPUT_PULLUP);
  pinMode(PIN_FLOW, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_FLOW), onPulse, FALLING);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  prefs.begin("pcal", false);
  p1ZeroV = prefs.getFloat("p1z", p1ZeroV);
  p2ZeroV = prefs.getFloat("p2z", p2ZeroV);
  runNum = prefs.getUInt("run", 0);

  printHelp();
  lastMs = millis();
  lastPulses = pulseCount;
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      inbuf.trim();
      if (inbuf == "r") { totalL = 0; Serial.println("Total reset."); }
      else if (inbuf == "p") { peak1 = peak2 = -1e9; Serial.println("Peaks reset."); }
      else if (inbuf == "z") {
        float s1 = 0, s2 = 0;
        for (int i = 0; i < 16; i++) { s1 += readVolts(PIN_P1); s2 += readVolts(PIN_P2); delay(20); }
        p1ZeroV = s1 / 16; p2ZeroV = s2 / 16;
        prefs.putFloat("p1z", p1ZeroV); prefs.putFloat("p2z", p2ZeroV);
        peak1 = peak2 = -1e9;
        Serial.printf("Zeroed: P1 %.4f V, P2 %.4f V (saved)\n", p1ZeroV, p2ZeroV);
      }
      else if (inbuf == "zc") {
        p1ZeroV = P1_ZERO_V_DEFAULT; p2ZeroV = P2_ZERO_V_DEFAULT;
        prefs.remove("p1z"); prefs.remove("p2z");
        peak1 = peak2 = -1e9;
        Serial.println("Saved zero cleared (back to defaults in main.cpp).");
      }
      else if (inbuf == "s") startRun();
      else if (inbuf == "x") stopRun();
      else if (inbuf == "h") printHelp();
      else if (inbuf.startsWith("i ")) {
        long ms = inbuf.substring(2).toInt();
        if (ms >= 100 && ms <= 10000) {
          updateMs = ms;
          Serial.printf("Interval %lu ms.\n", (unsigned long)updateMs);
        } else {
          Serial.println("Interval must be 100-10000 ms.");
        }
      }
      inbuf = "";
    } else if (inbuf.length() < 32) {
      inbuf += ch;
    }
  }

  pollButton();

  uint32_t now = millis();
  if (now - lastMs < updateMs) return;

  float dt = (now - lastMs) / 1000.0f;
  uint32_t p = pulseCount;
  float hz = (p - lastPulses) / dt;
  lastMs = now;
  lastPulses = p;

  float lpm = hzToLpm(hz);
  totalL += lpm * dt / 60.0f;

  float v1 = readVolts(PIN_P1);
  float v2 = readVolts(PIN_P2);
  float psi1 = voltsToPsi(v1, p1ZeroV, P1_FULLSCALE_PSI);
  float psi2 = voltsToPsi(v2, p2ZeroV, P2_FULLSCALE_PSI);
  if (psi1 > peak1) peak1 = psi1;
  if (psi2 > peak2) peak2 = psi2;

  char runStr[32];
  float runS = (now - runStartMs) / 1000.0f;
  if (running) snprintf(runStr, sizeof runStr, "RUN %lu %.3f", (unsigned long)runNum, runS);
  else snprintf(runStr, sizeof runStr, "IDLE");

  Serial.printf("%.2f Hz  %.2f L/min  total %.3f L  P1 %.2f psi  P2 %.2f psi  (%.3fV %s, %.3fV %s)  peak P1 %.2f P2 %.2f  t %.3f  %s\n",
    hz, lpm, totalL, psi1, psi2, v1, pStatus(v1), v2, pStatus(v2), peak1, peak2, now / 1000.0f, runStr);

  char a[32], b[32], c[32], d[32];
  if (page == 0) {
    if (running) {
      uint32_t s = (uint32_t)runS;
      snprintf(a, sizeof a, "RUN %lu %lu:%02lu %.1fHz", (unsigned long)runNum,
        (unsigned long)(s / 60), (unsigned long)(s % 60), hz);
    } else {
      snprintf(a, sizeof a, "Flow %.1f Hz", hz);
    }
    snprintf(b, sizeof b, "%.2f L/min%s", lpm, (hz > 0 && hz < HZ_MIN_VALID) ? " ~" : "");
    snprintf(c, sizeof c, "Total %.2f L", totalL);
    snprintf(d, sizeof d, "P1 %.1f P2 %.1f", psi1, psi2);
  } else if (page == 2) {
    snprintf(a, sizeof a, running ? "PEAK (this run)" : "PEAK  (serial p=reset)");
    snprintf(b, sizeof b, "P1 max %.1f psi", peak1);
    snprintf(c, sizeof c, "P2 max %.1f psi", peak2);
    snprintf(d, sizeof d, "now %.1f / %.1f", psi1, psi2);
  } else {
    snprintf(a, sizeof a, "P1 %.2fV %s", v1, pStatus(v1));
    snprintf(b, sizeof b, "   %.1f psi", psi1);
    snprintf(c, sizeof c, "P2 %.2fV %s", v2, pStatus(v2));
    snprintf(d, sizeof d, "   %.1f psi", psi2);
  }
  drawLines(a, b, c, d);
}