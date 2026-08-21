#include <Adafruit_NeoPixel.h>
#include <Wire.h>
#include <Updater.h>
#include <LittleFS.h>
#include <Wire.h>

// ── Pin config ────────────────────────────────────────────────────────────────
#define RGB_PIN     16
#define RING_PIN     2
#define MATRIX_PIN   3
#define STRIPL_PIN   4
#define STRIPR_PIN   5
#define RING_LEDS   24
#define MATRIX_LEDS 64
#define STRIP_LEDS  26

#define POLOLU_OFF_PIN 14

// INA226 I2C (same pins as old MAX17043)
#define INA226_ADDR 0x40
#define INA226_SDA  6
#define INA226_SCL  7

// INA226 registers
#define INA226_REG_CONFIG    0x00
#define INA226_REG_SHUNT     0x01
#define INA226_REG_BUS       0x02
#define INA226_REG_CALIB     0x05

// Shunt resistor value (R002 = 2mΩ = 0.002Ω)
#define SHUNT_OHMS        0.002f
// Pack capacity in mAh (3 x 2900mAh in series)
#define PACK_CAPACITY_MAH 2900.0f
// Current sense LSB with R002 shunt: 2.5uV / 0.002Ω = 1.25mA per LSB
#define CURRENT_LSB_MA    1.25f
// Bus voltage LSB is 1.25mV; calibrated scale factor matches physical multimeter reading (12.70V / 12.233V)
#define BUS_VOLTAGE_LSB      0.00125f // V per LSB
#define VOLTAGE_CALIB_SCALE  1.038168f // Calibrates VBUS pin reading to multimeter 12.70V

// 3S pack voltage thresholds
#define PACK_FULL_V          12.60f   // Standard 3S Li-ion full voltage (4.20V/cell)
#define PACK_EMPTY_V          9.00f   // 3.0V per cell
#define PACK_REST_I_MA       50.00f   // below this = at rest (no significant load/charge)
#define CHARGE_TAPER_MA      150.00f  // CV phase taper threshold → set SoC = 100%

#define FIRMWARE_VERSION "1.1.7"

// Buttons (INPUT_PULLDOWN, HIGH when pressed)
#define BTN_PREV    29
#define BTN_PLAY    28
#define BTN_NEXT    27
#define BTN_VOLUP   26
#define BTN_VOLDOWN 15

#define PI_SHUTDOWN_SENSE_PIN 8

#define DEBOUNCE_MS  50
#define VOL_STEP      5
#define VOL_MIN       0
#define VOL_MAX     100

Adafruit_NeoPixel rgb(1,            RGB_PIN,    NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel ring(RING_LEDS,   RING_PIN,   NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel matrix(MATRIX_LEDS, MATRIX_PIN, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel stripL(STRIP_LEDS, STRIPL_PIN, NEO_GRBW + NEO_KHZ800);
Adafruit_NeoPixel stripR(STRIP_LEDS, STRIPR_PIN, NEO_GRBW + NEO_KHZ800);

// ── State machine ─────────────────────────────────────────────────────────────
enum State { S_OFF, S_TAG_ON_BURST, S_PLAYING, S_PAUSED, S_TAG_OFF_FADE, S_VOLUME, S_BOOTING, S_SHUTDOWN, S_LED_TEST, S_IDLE, S_WIFI_AP };
State currentState   = S_BOOTING;
State preVolumeState = S_OFF;

// Vol Up button long-press tracking (for Pololu soft switch OFF trigger)
unsigned long volUpPressStart = 0;
bool volUpHeld = false;
bool shutdownInitiated = false;

// Pi shutdown polling state
unsigned long lastPiPingSent = 0;
unsigned long lastPiResponseTime = 0;
bool piNoResponseDetected = false;
unsigned long noResponseStartTime = 0;
bool ledsCleared = false;

unsigned long stateStart = 0;
unsigned long lastFrame  = 0;

void reportSoC();
String extractValue(const String& json, const String& key);
float   spinPos    = 0.0;
float   breathVal  = 1.0;
int     breathDir  = -1;
float   fadeVal    = 1.0;
int     volumeLevel = 80;
uint8_t animR = 0, animG = 200, animB = 200;
float   waveOffset = 0.0;

// ── SoC / INA226 state ────────────────────────────────────────────────────────
float   socPercent   = 50.0f;  // initial guess until we correct from voltage
bool    socValid     = false;
float   packVoltage  = 0.0f;
float   packCurrentMA = 0.0f;  // positive = discharging, negative = charging
bool    isCharging   = false;
bool    lastCharging = false;

unsigned long lastSoCRead    = 0;
unsigned long lastSoCReport  = 0;
unsigned long lastRestStart  = 0;
bool          atRest         = false;

#define SOC_READ_INTERVAL_MS    500    // read INA226 every 500ms
#define SOC_REPORT_INTERVAL_MS  2000   // report to Pi every 2s

// ── Buttons ───────────────────────────────────────────────────────────────────
struct Button {
  uint8_t pin;
  const char* name;
  bool state;
  bool lastRaw;
  unsigned long lastChange;
};

Button buttons[] = {
  { BTN_PREV,    "prev",   false, false, 0 },
  { BTN_PLAY,    "play",   false, false, 0 },
  { BTN_NEXT,    "next",   false, false, 0 },
  { BTN_VOLUP,   "vol_up", false, false, 0 },
  { BTN_VOLDOWN, "vol_dn", false, false, 0 },
};
#define NUM_BUTTONS 5

// ── INA226 ────────────────────────────────────────────────────────────────────
void reportSoC();
void ina226_writeReg(uint8_t reg, uint16_t val) {
  Wire1.beginTransmission(INA226_ADDR);
  Wire1.write(reg);
  Wire1.write((val >> 8) & 0xFF);
  Wire1.write(val & 0xFF);
  Wire1.endTransmission();
}

uint16_t ina226_readReg(uint8_t reg) {
  Wire1.beginTransmission(INA226_ADDR);
  Wire1.write(reg);
  Wire1.endTransmission(false);
  Wire1.requestFrom(INA226_ADDR, 2);
  if (Wire1.available() < 2) return 0;
  uint16_t val = (Wire1.read() << 8) | Wire1.read();
  return val;
}

void ina226_init() {
  Wire1.setSDA(INA226_SDA);
  Wire1.setSCL(INA226_SCL);
  Wire1.begin();

  // Config: avg 16 samples, 1.1ms conversion, continuous shunt+bus
  // BADC=SADC=0b0101 (1.1ms), AVG=0b011 (16), MODE=111 (continuous)
  ina226_writeReg(INA226_REG_CONFIG, 0x4527);

  // Calibration register: Cal = 0.00512 / (CurrentLSB * Rshunt)
  // CurrentLSB = 0.025mA = 0.000025A
  // Cal = 0.00512 / (0.000025 * 0.1) = 2048
  ina226_writeReg(INA226_REG_CALIB, 25600);

  Serial.println("INA226 init done");
}

bool ina226_scan() {
  Wire1.beginTransmission(INA226_ADDR);
  return Wire1.endTransmission() == 0;
}

// Returns bus voltage in volts
float ina226_voltage() {
  uint16_t raw = ina226_readReg(INA226_REG_BUS);
  return (raw * BUS_VOLTAGE_LSB) * VOLTAGE_CALIB_SCALE;
}

// Returns current in mA (positive = discharging, negative = charging)
float ina226_current() {
  int16_t raw = (int16_t)ina226_readReg(INA226_REG_SHUNT);
  // Shunt register LSB = 2.5uV
  // Current = (raw * 2.5uV) / Rshunt
  float shuntVoltage_uV = raw * 2.5f;
  return (shuntVoltage_uV / 1000.0f) / SHUNT_OHMS;
}

// ── SoC from OCV curve (3S NCA Li-ion, rest state only) ──────────────────────
// 11-point lookup table at 10% SoC intervals, voltages in V for 3S pack
// Based on standard NCA OCV curve (BAK N18650CL-29 type)
static const float OCV_V[11] = {
   9.00f,  // 0%
   9.84f,  // 10%
  10.50f,  // 20%
  10.86f,  // 30%
  11.10f,  // 40%
  11.25f,  // 50%
  11.40f,  // 60%
  11.55f,  // 70%
  11.76f,  // 80%
  12.06f,  // 90%
  12.60f,  // 100% (4.20V per cell)
};

float voltageToSoC(float v) {
  // Deduct internal resistance voltage drop during charging (I * 0.10 ohm)
  float v_ocv = isCharging ? (v - (abs(packCurrentMA) / 1000.0f) * 0.10f) : v;
  if (v_ocv <= OCV_V[0])  return 0.0f;
  if (v_ocv >= OCV_V[10]) return 100.0f;
  for (int i = 0; i < 10; i++) {
    if (v_ocv <= OCV_V[i + 1]) {
      float t = (v_ocv - OCV_V[i]) / (OCV_V[i + 1] - OCV_V[i]);
      return (i + t) * 10.0f;
    }
  }
  return 100.0f;
}

// ── SoC persistence (LittleFS) ────────────────────────────────────────────────
static float lastSavedSoC = -1.0f;

void saveSoC() {
  if (abs(socPercent - lastSavedSoC) >= 1.0f) {
    lastSavedSoC = socPercent;
    File f = LittleFS.open("/soc.json", "w");
    if (f) {
      f.printf("{\"soc\":%.1f}", socPercent);
      f.close();
    }
  }
}

void loadSoC() {
  packVoltage = ina226_voltage();
  socPercent  = voltageToSoC(packVoltage);
  lastSavedSoC = socPercent;
  socValid    = true;
  Serial.printf("Initial SoC from calibrated voltage: %.1f%%\n", socPercent);
}

// ── Volume persistence (LittleFS) ─────────────────────────────────────────────
static int lastSavedVolume = -1;

void saveVolume(int vol) {
  vol = constrain(vol, VOL_MIN, VOL_MAX);
  volumeLevel = vol;
  if (volumeLevel != lastSavedVolume) {
    lastSavedVolume = volumeLevel;
    File f = LittleFS.open("/vol.json", "w");
    if (f) {
      f.printf("{\"vol\":%d}", volumeLevel);
      f.close();
      Serial.printf("Saved volume to LittleFS: %d\n", volumeLevel);
    }
  }
}

void loadVolume() {
  if (LittleFS.exists("/vol.json")) {
    File f = LittleFS.open("/vol.json", "r");
    if (f) {
      String content = f.readString();
      f.close();
      int val = extractValue(content, "vol").toInt();
      if (val >= VOL_MIN && val <= VOL_MAX) {
        volumeLevel = val;
        lastSavedVolume = volumeLevel;
        Serial.printf("Loaded persistent volume from LittleFS: %d\n", volumeLevel);
        return;
      }
    }
  }
  volumeLevel = 80;
  lastSavedVolume = 80;
}

// ── SoC update (coulomb counting + correction) ────────────────────────────────
void updateSoC() {
  unsigned long now = millis();
  if (now - lastSoCRead < SOC_READ_INTERVAL_MS) return;

  float dt_h = (now - lastSoCRead) / 3600000.0f;  // ms to hours
  lastSoCRead = now;

  packVoltage   = ina226_voltage();
  packCurrentMA = ina226_current();
  isCharging    = packCurrentMA < -PACK_REST_I_MA;

  // Immediate report on charging state change
  if (isCharging != lastCharging) {
    lastCharging = isCharging;
    socValid = true;
    reportSoC();
  }

  // Coulomb counting: discharge = positive current = decreases SoC
  // charge = negative current = increases SoC
  float deltaSoC = (-packCurrentMA * dt_h / PACK_CAPACITY_MAH) * 100.0f;
  socPercent = constrain(socPercent + deltaSoC, 0.0f, 100.0f);

  // Rest state detection (ONLY when not charging)
  if (!isCharging && abs(packCurrentMA) < PACK_REST_I_MA) {
    if (!atRest) { atRest = true; lastRestStart = now; }
    // Correct from voltage after 30s at rest
    if (now - lastRestStart > 30000) {
      float voltSoC = voltageToSoC(packVoltage);
      // Gentle correction — blend toward voltage-based SoC
      socPercent = socPercent * 0.7f + voltSoC * 0.3f;
      Serial.print("OCV correction: ");
      Serial.print(voltSoC, 1); Serial.println("%");
    }
  } else {
    atRest = false;
    lastRestStart = now;
  }

  // Full charge anchor: High voltage (>=12.45V) + tapering/low current
  if (packVoltage >= 12.45f && abs(packCurrentMA) < CHARGE_TAPER_MA) {
    socPercent = 100.0f;
    Serial.println("Full charge / CV taper detected -> SoC = 100%");
  }

  // Empty anchor
  if (packVoltage <= PACK_EMPTY_V + 0.1f && !isCharging) {
    socPercent = 0.0f;
  }

  // Save to LittleFS if changed
  saveSoC();

  // Report to Pi periodically
  if (now - lastSoCReport >= SOC_REPORT_INTERVAL_MS || !socValid) {
    socValid = true;
    reportSoC();
  }
}

void reportSoC() {
  lastSoCReport = millis();
  String msg = "{\"event\":\"SOC\""
               ",\"version\":\"" FIRMWARE_VERSION "\""
               ",\"level\":"    + String((int)socPercent) +
               ",\"voltage\":"  + String(packVoltage, 3) +
               ",\"current\":"  + String(packCurrentMA, 1) +
               ",\"charging\":" + (isCharging ? "true" : "false") + "}";
  Serial1.println(msg);
  Serial.println(msg);
}

// ── 5x3 pixel font ────────────────────────────────────────────────────────────
const uint8_t digitFont[10][5] = {
  {0b111,0b101,0b101,0b101,0b111},{0b010,0b110,0b010,0b010,0b111},
  {0b111,0b001,0b111,0b100,0b111},{0b111,0b001,0b111,0b001,0b111},
  {0b101,0b101,0b111,0b001,0b001},{0b111,0b100,0b111,0b001,0b111},
  {0b111,0b100,0b111,0b101,0b111},{0b111,0b001,0b001,0b001,0b001},
  {0b111,0b101,0b111,0b101,0b111},{0b111,0b101,0b111,0b001,0b111},
};

// ── Matrix helpers ────────────────────────────────────────────────────────────
int matrixPixel(int x, int y) {
  return (y % 2 == 0) ? y * 8 + x : y * 8 + (7 - x);
}
void matrixSet(int x, int y, uint8_t r, uint8_t g, uint8_t b) {
  if (x < 0 || x >= 8 || y < 0 || y >= 8) return;
  matrix.setPixelColor(matrixPixel(x, y), matrix.Color(r, g, b));
}
void matrixClear() { matrix.clear(); }

void drawDigit(int d, int startX, int startY, uint8_t r, uint8_t g, uint8_t b) {
  for (int row = 0; row < 5; row++) {
    uint8_t bits = digitFont[d][row];
    for (int col = 0; col < 3; col++)
      if (bits & (1 << (2 - col))) matrixSet(startX + col, startY + row, r, g, b);
  }
}

void drawSoC(int soc) {
  matrixClear();
  uint8_t r, g, b = 0;
  if (isCharging)    { r = 0;   g = 200; b = 255; }
  else if (soc > 50) { r = 0;   g = 255; b = 0;   }
  else if (soc > 20) { r = 255; g = 180; b = 0;   }
  else               { r = 255; g = 0;   b = 0;   }

  if (soc >= 100) {
    drawDigit(1, 0, 1, r, g, b);
    drawDigit(0, 4, 1, r, g, b);
  } else if (soc < 10) {
    drawDigit(soc, 3, 1, r, g, b);
  } else {
    drawDigit(soc / 10, 1, 1, r, g, b);
    drawDigit(soc % 10, 5, 1, r, g, b);
  }

  matrixSet(7, 5, r, g, b);
  matrixSet(6, 6, r, g, b);
  if (isCharging) {
    float p = (sin(millis() * 0.006f) + 1.0f) * 0.5f;
    matrixSet(7, 7, 0, (uint8_t)(255 * p), (uint8_t)(100 * p));
  } else {
    matrixSet(7, 7, r, g, b);
  }
  matrix.show();

  int litLeds = (soc * RING_LEDS) / 100;
  ring.clear();
  float ringBreath = (sin(millis() * 0.004f) + 1.0f) * 0.5f;
  for (int i = 0; i < litLeds; i++) {
    float t = (float)i / RING_LEDS;
    uint8_t lr, lg, lb = 0;
    if (isCharging) {
      float pulse = 0.6f + 0.4f * ringBreath;
      lr = 0;
      lg = (uint8_t)(180 * pulse);
      lb = (uint8_t)(255 * pulse * t);
    } else {
      lr = t < 0.5f ? (uint8_t)(t * 2 * 80) : 255;
      lg = t < 0.5f ? 255 : (uint8_t)(255 - ((t - 0.5f) * 2 * 255));
    }
    ring.setPixelColor(i, ring.Color(lr, lg, lb));
  }
  ring.show();
}

// ── Matrix animations ─────────────────────────────────────────────────────────
void drawVolumeBar(int vol) {
  matrixClear();
  int cols = (vol * 8) / 100;
  for (int x = 0; x < cols; x++) {
    uint8_t r, g, b = 0;
    if (x < 4) { r = (uint8_t)(x * 60); g = 255; }
    else        { r = 255; g = (uint8_t)(255 - ((x - 4) * 60)); }
    for (int y = 0; y < 8; y++) matrixSet(x, y, r, g, b);
  }
  matrix.show();
}

void drawMatrixWave(uint8_t r, uint8_t g, uint8_t b) {
  matrixClear();
  for (int x = 0; x < 8; x++) {
    float phase  = waveOffset + (x * 0.8);
    float s      = (sin(phase) + 1.0) / 2.0;
    int litRows  = (int)(s * 8);
    int startRow = (8 - litRows) / 2;
    for (int y = startRow; y < startRow + litRows; y++) {
      float rowFade = 1.0 - abs((y - 3.5) / 4.0) * 0.5;
      matrixSet(x, y, (uint8_t)(r*rowFade), (uint8_t)(g*rowFade), (uint8_t)(b*rowFade));
    }
  }
  matrix.show();
}

void drawMatrixCheckerboard(uint8_t r, uint8_t g, uint8_t b) {
  matrixClear();
  for (int x = 0; x < 8; x++)
    for (int y = 0; y < 8; y++)
      if ((x + y) % 2 == 0) matrixSet(x, y, r, g, b);
  matrix.show();
}

void drawMatrixBurst(float progress, uint8_t r, uint8_t g, uint8_t b) {
  matrixClear();
  float dist = progress * 5.66;
  for (int x = 0; x < 8; x++) {
    for (int y = 0; y < 8; y++) {
      float dx = x - 3.5, dy = y - 3.5;
      float d  = sqrt(dx*dx + dy*dy);
      if (d <= dist) {
        float df = 1.0 - (d / dist) * 0.5;
        matrixSet(x, y, (uint8_t)(r*df), (uint8_t)(g*df), (uint8_t)(b*df));
      }
    }
  }
  matrix.show();
}

void drawMatrixSolid(uint8_t r, uint8_t g, uint8_t b) {
  for (int i = 0; i < MATRIX_LEDS; i++)
    matrix.setPixelColor(i, matrix.Color(r, g, b));
  matrix.show();
}

void drawMatrixSpin(float angle, uint8_t r, uint8_t g, uint8_t b) {
  matrixClear();
  for (int x = 0; x < 8; x++) {
    for (int y = 0; y < 8; y++) {
      float dx = x - 3.5f;
      float dy = y - 3.5f;
      float dist = sqrt(dx*dx + dy*dy);
      if (dist > 4.2f) continue;
      float a = atan2(dy, dx);
      float diff = a - angle;
      while (diff < 0) diff += 2.0f * M_PI;
      while (diff >= 2.0f * M_PI) diff -= 2.0f * M_PI;
      float norm = diff / (2.0f * M_PI);
      float tail = pow(1.0f - norm, 2.5f);
      matrixSet(x, y, (uint8_t)(r * tail), (uint8_t)(g * tail), (uint8_t)(b * tail));
    }
  }
  matrix.show();
}

void drawRingSpin(float angle, uint8_t r, uint8_t g, uint8_t b) {
  ring.clear();
  int lead = (int)((angle / (2.0f * M_PI)) * RING_LEDS) % RING_LEDS;
  if (lead < 0) lead += RING_LEDS;
  for (int i = 0; i < 8; i++) {
    int idx = (lead - i + RING_LEDS) % RING_LEDS;
    float fade = pow(1.0f - ((float)i / 8.0f), 2.0f);
    ring.setPixelColor(idx, ring.Color((uint8_t)(r * fade), (uint8_t)(g * fade), (uint8_t)(b * fade)));
  }
  ring.show();
}

// ── Helpers ───────────────────────────────────────────────────────────────────
void setRGB(uint8_t r, uint8_t g, uint8_t b) {
  rgb.setPixelColor(0, rgb.Color(r, g, b)); rgb.show();
}
void allOff() {
  ring.clear(); ring.show(); 
  matrixClear(); matrix.show(); 
  stripL.clear(); stripL.show();
  stripR.clear(); stripR.show();
  setRGB(0,0,0);
}

void setState(State s) {
  currentState = s;
  stateStart   = millis();
  lastFrame    = millis();
  if (s == S_OFF)          allOff();
  if (s == S_TAG_OFF_FADE) fadeVal   = 1.0;
  if (s == S_PLAYING)      { spinPos = 0; breathVal = 1.0; breathDir = -1; waveOffset = 0; }
  if (s == S_PAUSED)       { breathVal = 1.0; breathDir = -1; }
  if (s == S_SHUTDOWN) {
    lastPiPingSent = 0;
    lastPiResponseTime = millis();
    piNoResponseDetected = false;
    noResponseStartTime = 0;
    ledsCleared = false;
    ring.setBrightness(255);
    matrix.setBrightness(255);
    stripL.setBrightness(255);
    stripR.setBrightness(255);
  }
}

void overlaySoC() {
  if (!socValid) return;
  drawSoC((int)socPercent);
}

void frameShutdown() {
  unsigned long now = millis();
  unsigned long elapsed = now - stateStart;

  // Print shutdown countdown status every second to USB Serial
  static unsigned long lastPrint = 0;
  if (now - lastPrint >= 1000) {
    lastPrint = now;
    Serial.print("Pico: Shutdown state active. Elapsed: ");
    Serial.print(elapsed / 1000);
    Serial.println("s");
  }

  // 1. LED Feedback: Radar spinner animation while Pi shuts down
  float angle = (float)(now % 1200) / 1200.0f * 2.0f * M_PI;
  drawMatrixSpin(angle, 255, 80, 0); // High-intensity warm amber spinning radar
  drawRingSpin(angle, 255, 80, 0);
  stripL.clear(); stripR.clear(); stripL.show(); stripR.show();
  setRGB(255, 80, 0);

  // 2. Pi Polling logic
  if (!piNoResponseDetected) {
    // Send PING every 500ms
    if (now - lastPiPingSent >= 500) {
      lastPiPingSent = now;
      Serial1.println("{\"event\":\"PING\"}");
    }

    // Check if the Pi has stopped responding (timeout: 3000ms)
    if (now - lastPiResponseTime > 3000) {
      piNoResponseDetected = true;
      noResponseStartTime = now;
      Serial.println("Pico: Pi stopped responding. Initiating grace period...");
    }
  }

  // 3. Power Cut Decision
  bool cutPower = false;
  
  // Hardware signal from Pi gpio-poweroff overlay (Pi GPIO 16 -> Pico GP8)
  // (Guard with elapsed >= 6000ms so shutdown sound plays completely & Pi halts cleanly)
  if (elapsed >= 6000 && digitalRead(PI_SHUTDOWN_SENSE_PIN) == HIGH) {
    Serial.println("Pico: Hardware gpio-poweroff signal received from Pi. Cutting power immediately!");
    cutPower = true;
  }

  // Fallback UART polling grace period (8 seconds)
  if (piNoResponseDetected && (now - noResponseStartTime >= 8000)) {
    Serial.println("Pico: Grace period finished (8s). Cutting power.");
    cutPower = true;
  }
  
  // Hard safety timeout of 30 seconds
  if (elapsed >= 30000) {
    Serial.println("Pico: Hard safety timeout reached. Cutting power.");
    cutPower = true;
  }

  if (cutPower) {
    Serial.println("💥 [Pico] Initiating smooth LED fade-out before cutting power rails...");
    unsigned long fadeStart = millis();
    const unsigned long FADE_DURATION = 2000;
    while (millis() - fadeStart < FADE_DURATION) {
      float progress = (float)(millis() - fadeStart) / FADE_DURATION;
      float factor   = 1.0f - progress;
      if (factor < 0.0f) factor = 0.0f;

      uint8_t bRing   = (uint8_t)(255 * factor);
      uint8_t bMatrix = (uint8_t)(255 * factor);
      uint8_t bStrips = (uint8_t)(255 * factor);

      ring.setBrightness(bRing);
      matrix.setBrightness(bMatrix);
      stripL.setBrightness(bStrips);
      stripR.setBrightness(bStrips);

      float angle = (float)(millis() % 1200) / 1200.0f * 2.0f * M_PI;
      drawMatrixSpin(angle, 255, 80, 0);
      drawRingSpin(angle, 255, 80, 0);
      stripL.clear(); stripR.clear();
      setRGB((uint8_t)(255 * factor), (uint8_t)(80 * factor), 0);

      ring.show(); matrix.show(); stripL.show(); stripR.show();
      delay(15);
    }

    allOff();
    delay(50);
    Serial.println("💥 [Pico] DRIVING POLOLU_OFF_PIN (GP14) HIGH - CUTTING POWER RAILS!");
    Serial1.println("💥 [Pico] DRIVING POLOLU_OFF_PIN (GP14) HIGH - CUTTING POWER RAILS!");
    digitalWrite(POLOLU_OFF_PIN, HIGH);
    while (1) {
      delay(10);
    }
  }
}

void frameBooting() {
  unsigned long now = millis();
  
  // Beautiful flowing gradient wave traveling up the side strips
  float waveSpeed = 0.004f;
  float spatialFreq = 0.3f;
  
  stripL.clear();
  stripR.clear();
  
  for (int i = 0; i < STRIP_LEDS; i++) {
    float phase = (now * waveSpeed) - (i * spatialFreq);
    float s = (sin(phase) + 1.0f) * 0.5f; // 0.0 to 1.0
    
    // Transition from deep blue (s=0) to bright teal/cyan (s=1)
    uint8_t r = 0;
    uint8_t g = (uint8_t)(180 * s + 10 * (1.0f - s));
    uint8_t b = (uint8_t)(255 * s + 80 * (1.0f - s));
    uint8_t w = (uint8_t)(40 * s); // touch of warm white at peak intensity
    
    stripL.setPixelColor(i, stripL.Color(r, g, b, w));
    stripR.setPixelColor(i, stripR.Color(r, g, b, w));
  }
  stripL.show();
  stripR.show();
  
  // Cohesive breathing pulse on the ring
  float ringPhase = now * 0.002f;
  float ringBreath = (sin(ringPhase) + 1.0f) * 0.5f;
  uint8_t ringR = 0;
  uint8_t ringG = (uint8_t)(40 * ringBreath);
  uint8_t ringB = (uint8_t)(80 * ringBreath);
  
  ring.fill(ring.Color(ringR, ringG, ringB));
  ring.show();
  
  // Keep matrix clear during boot
  matrixClear();
  matrix.show();
  
  // Breath on status RGB
  setRGB(0, (uint8_t)(20 * ringBreath), (uint8_t)(40 * ringBreath));
}

// ── Animation frames ──────────────────────────────────────────────────────────
void frameBurst() {
  unsigned long elapsed = millis() - stateStart;
  float progress = elapsed / 1200.0;
  if (progress >= 1.0) { setState(S_PLAYING); return; }
  spinPos = fmod(spinPos + 0.8, RING_LEDS);
  ring.clear();
  for (int t = 0; t < 8; t++) {
    int pos = (int)(spinPos - t + RING_LEDS * 2) % RING_LEDS;
    float tf = 1.0 - (t / 8.0);
    ring.setPixelColor(pos, ring.Color((uint8_t)(animR*tf),(uint8_t)(animG*tf),(uint8_t)(animB*tf)));
  }
  ring.show();
  drawMatrixBurst(progress, animR, animG, animB);
  
  float stripFade = 1.0 - progress;
  stripL.fill(stripL.Color((uint8_t)(animR*stripFade), (uint8_t)(animG*stripFade), (uint8_t)(animB*stripFade)));
  stripR.fill(stripR.Color((uint8_t)(animR*stripFade), (uint8_t)(animG*stripFade), (uint8_t)(animB*stripFade)));
  stripL.show(); stripR.show();
  
  overlaySoC();
  setRGB(animR/2, animG/2, animB/2);
}

void framePlaying() {
  breathVal += 0.008 * breathDir;
  if (breathVal >= 1.0)  { breathVal = 1.0;  breathDir = -1; }
  if (breathVal <= 0.5)  { breathVal = 0.5;  breathDir =  1; }
  spinPos = fmod(spinPos + 0.15, RING_LEDS);
  ring.clear();
  int tailLen = (int)(10 * breathVal);
  for (int t = 0; t < tailLen; t++) {
    int pos = (int)(spinPos - t + RING_LEDS * 2) % RING_LEDS;
    float tf = 1.0 - (float)t / tailLen;
    ring.setPixelColor(pos, ring.Color((uint8_t)(animR*tf),(uint8_t)(animG*tf),(uint8_t)(animB*tf)));
  }
  ring.show();
  waveOffset += 0.08;
  drawMatrixWave(animR, animG, animB);
  
  // Breath effect for speaker strips
  stripL.clear(); stripR.clear();
  for (int i = 0; i < STRIP_LEDS; i++) {
    // Wave moving along the strip
    float phase = waveOffset + (i * 0.4); 
    float s = (sin(phase) + 1.0) / 2.0;
    
    // Combine wave with overall breath
    float intensity = s * breathVal;
    uint8_t r = (uint8_t)(animR * intensity);
    uint8_t g = (uint8_t)(animG * intensity);
    uint8_t b = (uint8_t)(animB * intensity);
    
    stripL.setPixelColor(i, stripL.Color(r, g, b, 0));
    stripR.setPixelColor(i, stripR.Color(r, g, b, 0));
  }
  stripL.show(); stripR.show();
  
  overlaySoC();
  setRGB(animR/4, animG/4, animB/4);
}

void framePaused() {
  breathVal += 0.003 * breathDir;
  if (breathVal >= 1.0)  { breathVal = 1.0;  breathDir = -1; }
  if (breathVal <= 0.3)  { breathVal = 0.3;  breathDir =  1; }
  int litLeds = (int)(RING_LEDS * breathVal);
  ring.clear();
  for (int i = 0; i < litLeds; i++)
    ring.setPixelColor(i, ring.Color(animR/2, animG/2, animB/2));
  ring.show();
  drawMatrixCheckerboard(animR, animG, animB);
  
  stripL.fill(stripL.Color((uint8_t)(animR*0.3), (uint8_t)(animG*0.3), (uint8_t)(animB*0.3)));
  stripR.fill(stripR.Color((uint8_t)(animR*0.3), (uint8_t)(animG*0.3), (uint8_t)(animB*0.3)));
  stripL.show(); stripR.show();
  
  overlaySoC();
  setRGB(animR/4, animG/4, animB/4);
}

void frameFade() {
  fadeVal -= 0.025;
  if (fadeVal <= 0) { setState(S_OFF); return; }
  ring.fill(ring.Color((uint8_t)(animR*fadeVal),(uint8_t)(animG*fadeVal),(uint8_t)(animB*fadeVal)));
  ring.show();
  drawMatrixSolid((uint8_t)(animR*fadeVal),(uint8_t)(animG*fadeVal),(uint8_t)(animB*fadeVal));
  
  stripL.fill(stripL.Color((uint8_t)(animR*fadeVal), (uint8_t)(animG*fadeVal), (uint8_t)(animB*fadeVal)));
  stripR.fill(stripR.Color((uint8_t)(animR*fadeVal), (uint8_t)(animG*fadeVal), (uint8_t)(animB*fadeVal)));
  stripL.show(); stripR.show();
  
  overlaySoC();
  setRGB((uint8_t)(animR*fadeVal*0.2),(uint8_t)(animG*fadeVal*0.2),(uint8_t)(animB*fadeVal*0.2));
}

void frameVolume() {
  if (millis() - stateStart > 2000) { setState(preVolumeState); return; }
  drawVolumeBar(volumeLevel);
  int litLeds = (volumeLevel * RING_LEDS) / 100;
  ring.clear();
  for (int i = 0; i < litLeds; i++) {
    float t   = (float)i / RING_LEDS;
    uint8_t r = t < 0.5 ? (uint8_t)(t*2*80) : 255;
    uint8_t g = t < 0.5 ? 255 : (uint8_t)(255-(t-0.5)*2*255);
    ring.setPixelColor(i, ring.Color(r, g, 0));
  }
  ring.show();
  
  int stripLit = (volumeLevel * STRIP_LEDS) / 100;
  stripL.clear(); stripR.clear();
  for (int i = 0; i < stripLit; i++) {
    float t   = (float)i / STRIP_LEDS;
    uint8_t r = t < 0.5 ? (uint8_t)(t*2*80) : 255;
    uint8_t g = t < 0.5 ? 255 : (uint8_t)(255-(t-0.5)*2*255);
    stripL.setPixelColor(i, stripL.Color(r, g, 0));
    stripR.setPixelColor(i, stripR.Color(r, g, 0));
  }
  stripL.show(); stripR.show();
}

void frameOff() {
  if (socValid) drawSoC((int)socPercent);
}

// ── Event handlers ────────────────────────────────────────────────────────────
void onReady() {
  String msg = "{\"event\":\"VOLUME\",\"level\":" + String(volumeLevel) + "}";
  Serial1.println(msg); Serial.println(msg);

  ring.fill(ring.Color(180,180,180));
  drawMatrixSolid(180,180,180);
  stripL.fill(stripL.Color(180,180,180));
  stripR.fill(stripR.Color(180,180,180));
  ring.show(); stripL.show(); stripR.show(); setRGB(180,180,180);
  delay(200);
  setState(S_OFF);
}

void onTagOn(bool mapped) {
  if (mapped) { animR = 0;   animG = 200; animB = 200; }
  else        { animR = 200; animG = 140; animB = 0;   }
  setState(S_TAG_ON_BURST);
}

void onPlaying(uint8_t r, uint8_t g, uint8_t b) {
  animR = r; animG = g; animB = b;
  setState(S_PLAYING);
}

void onVolume(int vol) {
  saveVolume(vol);
  preVolumeState = currentState;
  setState(S_VOLUME);
}

// ── JSON parser ───────────────────────────────────────────────────────────────
String extractValue(const String& json, const String& key) {
  String search = "\"" + key + "\"";
  int idx = json.indexOf(search);
  if (idx < 0) return "";
  idx = json.indexOf(':', idx + search.length());
  if (idx < 0) return "";
  idx++;
  while (idx < (int)json.length() && json[idx] == ' ') idx++;
  if (json[idx] == '"') {
    int start = idx + 1, end = json.indexOf('"', start);
    return json.substring(start, end);
  } else {
    int start = idx, end = start;
    while (end < (int)json.length() && json[end] != ',' && json[end] != '}') end++;
    return json.substring(start, end);
  }
}

String testTarget = "off";

void onLedTest(const String& target) {
  testTarget = target;
  if (target == "off" || target == "clear") {
    allOff();
    setState(S_IDLE);
    return;
  }

  // Set all NeoPixel strands to 100% brightness for hardware testing
  ring.setBrightness(255);
  matrix.setBrightness(255);
  stripL.setBrightness(255);
  stripR.setBrightness(255);

  allOff();
  setState(S_LED_TEST);
}

void frameLedTest() {
  unsigned long now = millis();
  unsigned long elapsed = now - stateStart;
  float angle = (float)(now % 1000) / 1000.0f * 2.0f * M_PI;

  if (testTarget == "matrix") {
    drawMatrixSpin(angle, 0, 240, 255);
    ring.clear(); ring.show();
    stripL.clear(); stripL.show();
    stripR.clear(); stripR.show();
    setRGB(0, 0, 0);
  } 
  else if (testTarget == "ring") {
    matrixClear(); matrix.show();
    // High-visibility spinning pulse on the Ring (supports both RGB and RGBW hardware)
    uint8_t pulse = (uint8_t)(128 + 127 * sin(now * 0.006f));
    ring.fill(ring.Color(255, pulse, 0));
    drawRingSpin(angle, 0, 255, 255);
    stripL.clear(); stripL.show();
    stripR.clear(); stripR.show();
    setRGB(0, 0, 0);
  } 
  else if (testTarget == "strip_l") {
    matrixClear(); matrix.show();
    ring.clear(); ring.show();
    stripL.fill(stripL.Color(0, 255, 150, 150)); stripL.show();
    stripR.clear(); stripR.show();
    setRGB(0, 0, 0);
  } 
  else if (testTarget == "strip_r") {
    matrixClear(); matrix.show();
    ring.clear(); ring.show();
    stripL.clear(); stripL.show();
    stripR.fill(stripR.Color(255, 150, 0, 150)); stripR.show();
    setRGB(0, 0, 0);
  } 
  else if (testTarget == "rgb") {
    matrixClear(); matrix.show();
    ring.clear(); ring.show();
    stripL.clear(); stripL.show();
    stripR.clear(); stripR.show();
    uint8_t step = (elapsed / 300) % 4;
    if (step == 0)      setRGB(255, 0, 0);
    else if (step == 1) setRGB(0, 255, 0);
    else if (step == 2) setRGB(0, 0, 255);
    else if (step == 3) setRGB(255, 255, 255);
  } 
  else if (testTarget == "all") {
    drawMatrixSpin(angle, 0, 240, 255);
    uint8_t pulse = (uint8_t)(128 + 127 * sin(now * 0.006f));
    ring.fill(ring.Color(255, pulse, 0));
    drawRingSpin(angle, 0, 255, 255);
    stripL.fill(stripL.Color(0, 255, 150, 150)); stripL.show();
    stripR.fill(stripR.Color(255, 150, 0, 150)); stripR.show();
    setRGB(255, 255, 255);
  }
}

void frameWifiAp() {
  unsigned long now = millis();
  float angle = (float)(now % 1500) / 1500.0f * 2.0f * M_PI;
  uint8_t pulse = (uint8_t)(100 + 155 * (0.5f + 0.5f * sin(now * 0.005f)));

  drawMatrixSpin(angle, 0, 200, 255);
  drawRingSpin(angle, 0, 220, 255);
  stripL.fill(stripL.Color(0, pulse, 255, 100));
  stripR.fill(stripR.Color(0, pulse, 255, 100));
  stripL.show(); stripR.show();
  setRGB(0, pulse, 255);
}

void handleEvent(const String& line) {
  Serial.print("Pi: "); Serial.println(line);
  String event = extractValue(line, "event");
  if      (event == "PING")     Serial1.println("{\"event\":\"PONG\",\"version\":\"" FIRMWARE_VERSION "\"}");
  else if (event == "PONG") {
    lastPiResponseTime = millis();
    Serial.println("Pico: received PONG from Pi.");
  }
  else if (event == "LED_TEST") {
    String target = extractValue(line, "target");
    onLedTest(target);
  }
  else if (event == "READY" || event == "IDLE") onReady();
  else if (event == "WIFI_AP") setState(S_WIFI_AP);
  else if (event == "PING") { Serial1.println("{\"event\":\"PONG\"}"); Serial.println("{\"event\":\"PONG\"}"); }
  else if (event == "TAG_ON")   onTagOn(extractValue(line, "mapped") == "true");
  else if (event == "TAG_OFF" || event == "TAG_UNKNOWN") setState(S_TAG_OFF_FADE);
  else if (event == "SHUTDOWN" || (event == "EVENT" && extractValue(line, "name") == "shutdown")) {
    setState(S_SHUTDOWN);
  }
  else if (event == "PLAYING") {
    String rs = extractValue(line, "r");
    String gs = extractValue(line, "g");
    String bs = extractValue(line, "b");
    onPlaying(
      rs.length() ? rs.toInt() : animR,
      gs.length() ? gs.toInt() : animG,
      bs.length() ? bs.toInt() : animB
    );
  }
  else if (event == "PAUSED") setState(S_PAUSED);
  else if (event == "VOLUME") {
    String lvl = extractValue(line, "level");
    if (lvl.length()) onVolume(lvl.toInt());
  }
  else if (event == "BRIGHTNESS") {
    String r = extractValue(line, "ring");
    String m = extractValue(line, "matrix");
    if (r.length()) { ring.setBrightness(r.toInt());   ring.show(); }
    if (m.length()) { matrix.setBrightness(m.toInt()); matrix.show(); }
  }
  else if (event == "ENTER_OTA") {
    Serial.println("Entering UART OTA mode...");
    
    // Flush any pending bytes in the RX buffer BEFORE sending OTA_READY
    while (Serial1.available()) Serial1.read();
    
    Serial1.println("{\"event\":\"OTA_READY\"}");
    
    while (!Serial1.available()) { delay(1); }
    String sizeStr = Serial1.readStringUntil('\n');
    sizeStr.trim();
    size_t fileSize = sizeStr.toInt();
    
    Serial1.printf("{\"event\":\"DEBUG\",\"sizeStr\":\"%s\",\"fileSize\":%d}\n", sizeStr.c_str(), fileSize);
    
    if (fileSize > 0) {
      bool ok = Update.begin(fileSize);
      Serial1.printf("{\"event\":\"DEBUG\",\"updateBegin\":%s}\n", ok ? "true" : "false");
      if (ok) {
        Serial1.println("{\"event\":\"OTA_BEGIN\"}");
        
        size_t written = 0;
        uint8_t buf[1024];
        
        while (written < fileSize) {
          size_t toRead = fileSize - written;
          if (toRead > sizeof(buf)) toRead = sizeof(buf);
          
          size_t chunkRead = 0;
          unsigned long lastData = millis();
          while (chunkRead < toRead && (millis() - lastData < 5000)) {
            if (Serial1.available()) {
              buf[chunkRead++] = Serial1.read();
              lastData = millis();
            }
          }
          
          if (chunkRead == toRead) {
            Update.write(buf, toRead);
            written += toRead;
            Serial1.println("ACK");
          } else {
            Serial1.println("NACK_TIMEOUT");
            break; // Abort on timeout
          }
        }
        
        if (written == fileSize && Update.end()) {
          Serial1.println("{\"event\":\"OTA_SUCCESS\"}");
          delay(100);
          rp2040.restart();
        } else {
          Serial1.println("{\"event\":\"OTA_FAILED\"}");
          rp2040.restart();
        }
      } else {
        Serial1.println("{\"event\":\"OTA_FAILED_BEGIN\"}");
      }
    } else {
      Serial1.println("{\"event\":\"OTA_FAILED_BEGIN\"}");
    }
  }
}

// ── Button handling ───────────────────────────────────────────────────────────
void sendButtonEvent(const char* name, bool pressed) {
  String msg = "{\"event\":\"BUTTON\",\"button\":\"" + String(name) +
               "\",\"pressed\":" + (pressed ? "true" : "false") + "}";
  Serial1.println(msg); Serial.println(msg);
}

void handleButtonPress(const char* name) {
  if (strcmp(name, "play") == 0) {
    if (currentState == S_PLAYING) {
      setState(S_PAUSED);
      Serial1.println("{\"event\":\"BUTTON_ACTION\",\"action\":\"pause\"}");
    } else if (currentState == S_PAUSED) {
      setState(S_PLAYING);
      Serial1.println("{\"event\":\"BUTTON_ACTION\",\"action\":\"resume\"}");
    }
  } else if (strcmp(name, "next") == 0) {
    Serial1.println("{\"event\":\"BUTTON_ACTION\",\"action\":\"next\"}");
  } else if (strcmp(name, "prev") == 0) {
    Serial1.println("{\"event\":\"BUTTON_ACTION\",\"action\":\"prev\"}");
  } else if (strcmp(name, "vol_up") == 0) {
    volumeLevel = constrain(volumeLevel + VOL_STEP, VOL_MIN, VOL_MAX);
    String msg = "{\"event\":\"BUTTON_ACTION\",\"action\":\"volume\",\"level\":" + String(volumeLevel) + "}";
    Serial1.println(msg); onVolume(volumeLevel);
  } else if (strcmp(name, "vol_dn") == 0) {
    volumeLevel = constrain(volumeLevel - VOL_STEP, VOL_MIN, VOL_MAX);
    String msg = "{\"event\":\"BUTTON_ACTION\",\"action\":\"volume\",\"level\":" + String(volumeLevel) + "}";
    Serial1.println(msg); onVolume(volumeLevel);
  }
}

void pollButtons() {
  unsigned long now = millis();
  for (int i = 0; i < NUM_BUTTONS; i++) {
    bool raw = digitalRead(buttons[i].pin) == LOW; // Active-low: LOW when pressed
    if (raw != buttons[i].lastRaw) { buttons[i].lastChange = now; buttons[i].lastRaw = raw; }
    if ((now - buttons[i].lastChange) >= DEBOUNCE_MS && raw != buttons[i].state) {
      buttons[i].state = raw;
      
      if (strcmp(buttons[i].name, "vol_up") == 0) {
        // Special Vol Up button active-low/long-press handling
        sendButtonEvent(buttons[i].name, raw);
        if (raw) {
          volUpPressStart = now;
          volUpHeld = true;
          shutdownInitiated = false;
        } else {
          volUpHeld = false;
          if (!shutdownInitiated) {
            handleButtonPress(buttons[i].name);
          }
          shutdownInitiated = false;
        }
      } else {
        // Standard buttons active-low
        sendButtonEvent(buttons[i].name, raw);
        if (raw) handleButtonPress(buttons[i].name);
      }
    }
  }

  // Monitor Vol Up long-press outside debounce loop
  if (volUpHeld && !shutdownInitiated) {
    if (now - volUpPressStart >= 3000) {
      shutdownInitiated = true;
      Serial.println("Volume Up button long-press detected! Sending SHUTDOWN to Pi.");
      Serial1.println("{\"event\":\"SHUTDOWN\"}");
      setState(S_SHUTDOWN);
    }
  }
}

// ── Setup & loop ──────────────────────────────────────────────────────────────
String inputBuffer = "";

void setup() {
  Serial.begin(115200);

  if (!LittleFS.begin()) {
    Serial.println("LittleFS mount failed!");
  } else {
    Serial.println("LittleFS mount success.");
    loadVolume();
  }

  rgb.begin();    rgb.setBrightness(80);
  ring.begin();   ring.setBrightness(60);
  matrix.begin(); matrix.setBrightness(40);
  stripL.begin(); stripL.setBrightness(100);
  stripR.begin(); stripR.setBrightness(100);
  allOff();
  delay(200);

  pinMode(BTN_PREV,    INPUT_PULLUP);
  pinMode(BTN_PLAY,    INPUT_PULLUP);
  pinMode(BTN_NEXT,    INPUT_PULLUP);
  pinMode(BTN_VOLUP,   INPUT_PULLUP);
  pinMode(BTN_VOLDOWN, INPUT_PULLUP);

  // Pololu OFF pin & Pi Shutdown Sense pin
  pinMode(POLOLU_OFF_PIN, OUTPUT);
  digitalWrite(POLOLU_OFF_PIN, LOW); // keep power rails alive
  pinMode(PI_SHUTDOWN_SENSE_PIN, INPUT_PULLDOWN);

  // Startup rainbow
  uint32_t colors[6] = {
    rgb.Color(200,0,0), rgb.Color(200,100,0), rgb.Color(0,200,0),
    rgb.Color(0,200,200), rgb.Color(0,0,200), rgb.Color(150,0,200)
  };
  for (int i = 0; i < 6; i++) {
    rgb.setPixelColor(0, colors[i]);
    ring.fill(colors[i]);
    stripL.fill(colors[i]);
    stripR.fill(colors[i]);
    for (int p = 0; p < MATRIX_LEDS; p++) matrix.setPixelColor(p, colors[i]);
    rgb.show(); ring.show(); matrix.show(); stripL.show(); stripR.show();
    delay(120);
  }
  allOff();

  // INA226 init
  ina226_init();
  if (ina226_scan()) {
    Serial.println("INA226 found at 0x40");
    loadSoC();
    reportSoC();
  } else {
    Serial.println("INA226 not found!");
  }

  lastSoCRead   = millis();
  lastSoCReport = millis();
  lastRestStart = millis();

  Serial1.setTX(0); Serial1.setRX(1); Serial1.begin(115200);

  // Start in booting state and notify the Pi
  setState(S_BOOTING);
  Serial1.print("{\"event\":\"BOOTING\",\"version\":\"" FIRMWARE_VERSION "\",\"volume\":");
  Serial1.print(volumeLevel);
  Serial1.println("}");
}

void loop() {
  // Animation frame ~60fps
  if (millis() - lastFrame >= 16) {
    lastFrame = millis();
    switch (currentState) {
      case S_BOOTING:      frameBooting(); break;
      case S_TAG_ON_BURST: frameBurst();   break;
      case S_PLAYING:      framePlaying(); break;
      case S_PAUSED:       framePaused();  break;
      case S_TAG_OFF_FADE: frameFade();    break;
      case S_VOLUME:       frameVolume();  break;
      case S_OFF:          frameOff();     break;
      case S_SHUTDOWN:     frameShutdown(); break;
      case S_LED_TEST:     frameLedTest(); break;
      case S_WIFI_AP:       frameWifiAp();  break;
      case S_IDLE:         allOff();       break;
    }
  }

  // SoC update
  updateSoC();

  // Buttons
  pollButtons();

  // UART from Pi
  while (Serial1.available()) {
    char c = (char)Serial1.read();
    if (c == '\n') {
      inputBuffer.trim();
      if (inputBuffer.length() > 0) handleEvent(inputBuffer);
      inputBuffer = "";
    } else {
      inputBuffer += c;
      if (inputBuffer.length() > 128) {
        inputBuffer = ""; // Prevent memory exhaustion from UART noise during shutdown
      }
    }
  }
}
