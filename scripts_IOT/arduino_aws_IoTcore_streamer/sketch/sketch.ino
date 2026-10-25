/*
  printer_monitor_v9 - MCU side (as v7; v9 only relabels the OLED:
  sensor #1 = TABLE, sensor #2 = HEAD/toolhead)
  ------------------------------------------------
  Streams both ADXL345s to the Linux side over the Bridge, where
  python/main.py serves them on a web page (port 7001).

    - ADXL345 #1 at 0x53, #2 at 0x1D, FIFO in stream mode, ODR_HZ each
    - Every BLOCK samples of one sensor -> Bridge.notify("accel", ...)
      args: seq, sensor (0/1), odr, base64(int16 LE x,y,z * BLOCK)
    - Linux -> MCU: notify("web_status", viewers) once a second

  v6 changes (v5 froze on the board):
    - no Bridge.call() in loop(): it waits forever if a reply is lost.
      Link status now comes from Linux's "web_status" notification.
    (v6 dropped Bridge.begin() - wrong: Monitor.begin() then locks the
     Bridge's mutexes before they are initialised and hangs. v7 calls
     Bridge.begin() first, then Monitor.begin(), as v1-v5 did.)
    - loop() yields (delay 2 ms) so the Bridge's own thread always runs.
    - smaller messages (one sensor, 24 samples, ~215 bytes) from a
      static buffer - no String building on the hot path.
    - at most 2 messages per loop pass.
    - OLED bottom row: tx / rx counters + spinner = loop is alive.
      During start-up the OLED shows which step it is on.

  Files: sketch.ino, stream_types.h, oled_text.h.
  Libraries: Arduino_RouterBridge only.
*/

#include <Arduino_RouterBridge.h>
#include <Wire.h>
#include "stream_types.h"
#include "oled_text.h"

// ---------- settings ----------
const uint16_t ODR_HZ = 400;          // 100, 200 or 400
const float    G_PER_LSB = 0.0039f;   // full-resolution mode
const unsigned long OLED_ROW_MS = 60; // one OLED row per 60 ms
const unsigned long MON_MS = 5000;    // Monitor summary interval
const uint8_t  MAX_MSG_PER_PASS = 2;

// ---------- ADXL345 registers ----------
const uint8_t REG_DEVID       = 0x00;
const uint8_t REG_BW_RATE     = 0x2C;
const uint8_t REG_POWER_CTL   = 0x2D;
const uint8_t REG_DATA_FORMAT = 0x31;
const uint8_t REG_DATAX0      = 0x32;
const uint8_t REG_FIFO_CTL    = 0x38;
const uint8_t REG_FIFO_STATUS = 0x39;
const uint8_t ADXL_DEVID      = 0xE5;

Adxl sensors[2];
uint8_t oledAddr = 0x3C;
bool    oledOk = false;

// written by the Bridge thread (web_status handler)
volatile int           webViewers = 0;
volatile unsigned long webStatusMs = 0;
volatile uint32_t      rxCount = 0;

uint32_t txCount = 0;
unsigned long lastProbe = 0, lastOledRow = 0, lastMon = 0;
unsigned long maxNotifyMs = 0;
uint8_t  oledRow = 0, spin = 0;
uint8_t  nextSensor = 0;

// ---------- Linux -> MCU ----------
void onWebStatus(int viewers) {
  webViewers = viewers;
  webStatusMs = millis();
  rxCount++;
}

bool linkOk() {
  return webStatusMs != 0 && (millis() - webStatusMs) < 3000;
}

// ---------- I2C helpers ----------
bool i2cPresent(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

bool writeReg(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool readRegs(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t len) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(addr, len) != len) return false;
  for (uint8_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

uint8_t rateCode(uint16_t hz) {
  switch (hz) {
    case 100: return 0x0A;
    case 200: return 0x0B;
    default:  return 0x0C;   // 400 Hz
  }
}

// ---------- ADXL345 ----------
void resetStats(Adxl &s) {
  for (uint8_t a = 0; a < 3; a++) { s.sum[a] = 0; s.sumsq[a] = 0; }
  s.n = 0;
}

bool adxlStart(Adxl &s) {
  s.present = false;
  s.count = 0;
  resetStats(s);
  if (!i2cPresent(s.addr)) return false;
  uint8_t id = 0;
  if (!readRegs(s.addr, REG_DEVID, &id, 1) || id != ADXL_DEVID) return false;

  writeReg(s.addr, REG_POWER_CTL, 0x00);            // standby while configuring
  writeReg(s.addr, REG_DATA_FORMAT, 0x0B);          // full res, +/-16 g
  writeReg(s.addr, REG_BW_RATE, rateCode(ODR_HZ));
  writeReg(s.addr, REG_FIFO_CTL, 0x00);             // bypass -> clears FIFO
  writeReg(s.addr, REG_FIFO_CTL, 0x80);             // stream mode
  writeReg(s.addr, REG_POWER_CTL, 0x08);            // measure
  s.present = true;
  return true;
}

void addStats(Adxl &s, const int16_t v[3]) {
  for (uint8_t a = 0; a < 3; a++) {
    float g = v[a] * G_PER_LSB;
    s.sum[a] += g;
    s.sumsq[a] += g * g;
  }
  if (++s.n >= ODR_HZ / 2) {                        // 0.5 s window
    float tot = 0;
    for (uint8_t a = 0; a < 3; a++) {
      float mean = s.sum[a] / s.n;
      float var = s.sumsq[a] / s.n - mean * mean;
      s.rms[a] = var > 0 ? sqrtf(var) : 0;
      tot += s.rms[a] * s.rms[a];
    }
    s.rmsTotal = sqrtf(tot);
    resetStats(s);
  }
}

// Empty the sensor's hardware FIFO into its buffer
void drain(Adxl &s) {
  if (!s.present) return;
  uint8_t st;
  if (!readRegs(s.addr, REG_FIFO_STATUS, &st, 1)) { s.present = false; return; }
  uint8_t entries = st & 0x3F;
  if (entries >= 32) s.fifoFull++;

  for (uint8_t i = 0; i < entries; i++) {
    uint8_t d[6];
    if (!readRegs(s.addr, REG_DATAX0, d, 6)) { s.present = false; return; }
    int16_t v[3] = {
      (int16_t)((d[1] << 8) | d[0]),
      (int16_t)((d[3] << 8) | d[2]),
      (int16_t)((d[5] << 8) | d[4])
    };
    addStats(s, v);
    if (s.count < BUF_CAP) {
      s.buf[s.count][0] = v[0];
      s.buf[s.count][1] = v[1];
      s.buf[s.count][2] = v[2];
      s.count++;
    } else {
      s.dropped++;
    }
  }
}

void drainAll() {
  drain(sensors[0]);
  drain(sensors[1]);
}

// ---------- base64 into a static buffer ----------
const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const size_t RAW_LEN = BLOCK * 6;                  // 144 bytes
static uint8_t raw[RAW_LEN];
static char    b64buf[(RAW_LEN + 2) / 3 * 4 + 1];   // 192 chars + NUL

void base64Encode(const uint8_t *p, size_t len, char *out) {
  size_t o = 0;
  for (size_t i = 0; i < len; i += 3) {
    uint32_t v = (uint32_t)p[i] << 16;
    if (i + 1 < len) v |= (uint32_t)p[i + 1] << 8;
    if (i + 2 < len) v |= p[i + 2];
    out[o++] = B64[(v >> 18) & 0x3F];
    out[o++] = B64[(v >> 12) & 0x3F];
    out[o++] = (i + 1 < len) ? B64[(v >> 6) & 0x3F] : '=';
    out[o++] = (i + 2 < len) ? B64[v & 0x3F] : '=';
  }
  out[o] = '\0';
}

// Send one block of one sensor
void sendBlock(uint8_t k) {
  Adxl &s = sensors[k];
  size_t n = 0;
  for (uint8_t i = 0; i < BLOCK; i++) {
    for (uint8_t a = 0; a < 3; a++) {
      uint16_t u = (uint16_t)s.buf[i][a];
      raw[n++] = u & 0xFF;
      raw[n++] = u >> 8;
    }
  }
  s.count -= BLOCK;
  memmove(s.buf, s.buf + BLOCK, s.count * sizeof(s.buf[0]));

  base64Encode(raw, n, b64buf);
  const char *payload = b64buf;

  unsigned long t0 = millis();
  Bridge.notify("accel", (int)s.seq, (int)k, (int)ODR_HZ, payload);
  unsigned long dt = millis() - t0;
  if (dt > maxNotifyMs) maxNotifyMs = dt;

  s.seq++;
  txCount++;
}

// Up to MAX_MSG_PER_PASS messages, alternating sensors
void sendSome() {
  for (uint8_t m = 0; m < MAX_MSG_PER_PASS; m++) {
    bool sent = false;
    for (uint8_t j = 0; j < 2 && !sent; j++) {
      uint8_t k = (nextSensor + j) % 2;
      if (sensors[k].present && sensors[k].count >= BLOCK) {
        sendBlock(k);
        nextSensor = (k + 1) % 2;
        sent = true;
      }
    }
    if (!sent) return;
    drainAll();
  }
}

// ---------- OLED ----------
bool startOled() {
  if (i2cPresent(0x3C))      oledAddr = 0x3C;
  else if (i2cPresent(0x3D)) oledAddr = 0x3D;
  else return false;
  return oled::begin(oledAddr);
}

void stage(const char *txt) {           // start-up progress
  Monitor.println(txt);
  if (oledOk) oled::line(2, txt);
}

// Float to text without printf %f (not always enabled in the core's libc)
void fmtFloat(char *out, size_t cap, float v, uint8_t dec) {
  bool neg = v < 0;
  if (neg) v = -v;
  uint32_t scale = 1;
  for (uint8_t i = 0; i < dec; i++) scale *= 10;
  uint32_t total = (uint32_t)(v * scale + 0.5f);
  uint32_t ip = total / scale, fp = total % scale;
  if (dec == 0) snprintf(out, cap, "%s%lu", neg ? "-" : "", (unsigned long)ip);
  else          snprintf(out, cap, "%s%lu.%0*lu", neg ? "-" : "", (unsigned long)ip,
                         (int)dec, (unsigned long)fp);
}

void fmt3(char *out, float g) {         // 0.0123 -> ".012", 1.234 -> "1.23"
  char t[12];
  if (g >= 9.995f)       fmtFloat(t, sizeof(t), g, 0);
  else if (g >= 0.9995f) fmtFloat(t, sizeof(t), g, 2);
  else { fmtFloat(t, sizeof(t), g, 3); memmove(t, t + 1, strlen(t)); }
  strcpy(out, t);
}

void drawOledRow(uint8_t row) {
  char ln[24];
  switch (row) {
    case 0: snprintf(ln, sizeof(ln), "VIB STREAM v9 %uHz", ODR_HZ); oled::line(0, ln); break;
    case 1: oled::hline(1); break;
    case 2: case 4: {
      uint8_t k = (row == 2) ? 0 : 1;
      Adxl &s = sensors[k];
      char g[12];
      fmtFloat(g, sizeof(g), s.rmsTotal, 4);
      const char *nm = k ? "HEAD " : "TABLE";
      if (!s.present) snprintf(ln, sizeof(ln), "%s %s MISSING", nm, k ? "1D" : "53");
      else            snprintf(ln, sizeof(ln), "%s Grms %sg", nm, g);
      oled::line(row, ln);
      break;
    }
    case 3: case 5: {
      Adxl &s = sensors[row == 3 ? 0 : 1];
      if (!s.present) { oled::line(row, ""); break; }
      char x[8], y[8], z[8];
      fmt3(x, s.rms[0]); fmt3(y, s.rms[1]); fmt3(z, s.rms[2]);
      snprintf(ln, sizeof(ln), " x%s y%s z%s", x, y, z);
      oled::line(row, ln);
      break;
    }
    case 6:
      if (linkOk()) snprintf(ln, sizeof(ln), "Web viewers %d", webViewers);
      else          snprintf(ln, sizeof(ln), "Linux: no status");
      oled::line(6, ln);
      break;
    case 7: {
      static const char SP[] = "|/-\\";
      spin = (spin + 1) % 4;
      snprintf(ln, sizeof(ln), "tx%lu rx%lu %c", (unsigned long)txCount,
               (unsigned long)rxCount, SP[spin]);
      oled::line(7, ln);
      break;
    }
  }
}

// ---------- main ----------
void setup() {
  Wire.begin();
  Wire.setClock(400000);

  oledOk = startOled();
  if (oledOk) {
    oled::line(0, "VIB STREAM v9");
    oled::line(2, "1 starting Bridge");
  }

  Bridge.begin();                        // must come first: initialises its locks
  if (oledOk) oled::line(2, "2 starting Monitor");
  Monitor.begin();
  stage("3 Bridge + Monitor up");

  if (Bridge.provide("web_status", onWebStatus)) stage("4 web_status OK");
  else                                           stage("4 provide FAILED");

  sensors[0].addr = 0x53;
  sensors[1].addr = 0x1D;
  adxlStart(sensors[0]);
  adxlStart(sensors[1]);
  stage("5 sensors set up");

  Monitor.println(String("TABLE (0x53) ") + (sensors[0].present ? "OK" : "missing") +
                  "  HEAD (0x1D) " + (sensors[1].present ? "OK" : "missing"));
  stage("6 streaming");
}

void loop() {
  unsigned long now = millis();

  drainAll();
  sendSome();

  // Re-probe missing devices every 2 s
  if (now - lastProbe >= 2000) {
    lastProbe = now;
    for (uint8_t k = 0; k < 2; k++) {
      if (!sensors[k].present && adxlStart(sensors[k])) {
        Monitor.println("ADXL345 #" + String(k + 1) + " (re)started");
      }
    }
    if (!oledOk) oledOk = startOled();
  }

  // One OLED row at a time keeps each I2C burst short
  if (oledOk && now - lastOledRow >= OLED_ROW_MS) {
    lastOledRow = now;
    drawOledRow(oledRow);
    oledRow = (oledRow + 1) % 8;
  }

  if (now - lastMon >= MON_MS) {
    lastMon = now;
    char ln[120], g1[12], g2[12];
    fmtFloat(g1, sizeof(g1), sensors[0].rmsTotal, 4);
    fmtFloat(g2, sizeof(g2), sensors[1].rmsTotal, 4);
    snprintf(ln, sizeof(ln),
             "tx %lu rx %lu maxNotify %lums S1 %s %sg S2 %s %sg full %lu drop %lu %s",
             (unsigned long)txCount, (unsigned long)rxCount, maxNotifyMs,
             sensors[0].present ? "ok" : "--", g1,
             sensors[1].present ? "ok" : "--", g2,
             (unsigned long)(sensors[0].fifoFull + sensors[1].fifoFull),
             (unsigned long)(sensors[0].dropped + sensors[1].dropped),
             linkOk() ? "link OK" : "no status");
    Monitor.println(ln);
    maxNotifyMs = 0;
  }

  delay(2);                              // let the Bridge thread run
}
