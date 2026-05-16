#include <Adafruit_NeoPixel.h>
#include <Wire.h>

// ── Pin config ────────────────────────────────────────────────────────────────
#define RGB_PIN     16
#define RING_PIN     2
#define MATRIX_PIN   3
#define RING_LEDS   24
#define MATRIX_LEDS 64

// INA226 I2C (same pins as old MAX17043)
#define INA226_ADDR 0x40
#define INA226_SDA  6
#define INA226_SCL  7

// INA226 registers
#define INA226_REG_CONFIG    0x00
#define INA226_REG_SHUNT     0x01
#define INA226_REG_BUS       0x02
#define INA226_REG_CALIB     0x05

// Shunt resistor value (R100 = 100mΩ = 0.1Ω)
#define SHUNT_OHMS        0.002f
// Pack capacity in mAh (3 x 2900mAh in series)
#define PACK_CAPACITY_MAH 2900.0f
// Current sense LSB with R100 shunt: 2.5uV / 0.1Ω = 25uA per LSB
#define CURRENT_LSB_MA    0.1f     // mA per LSB (after calibration)
// Bus voltage LSB is always 1.25mV
#define BUS_VOLTAGE_LSB   0.00125f // V per LSB

// 3S pack voltage thresholds
#define PACK_FULL_V       12.6f
#define PACK_EMPTY_V       9.0f   // 3.0V per cell
#define PACK_REST_I_MA    50.0f   // below this = at rest (no significant load/charge)
#define CHARGE_TAPER_MA   150.0f  // CV phase taper threshold → set SoC = 100%

// Buttons (INPUT_PULLDOWN, HIGH when pressed)
#define BTN_PREV    29
#define BTN_PLAY    28
#define BTN_NEXT    27
#define BTN_VOLUP   26
#define BTN_VOLDOWN 15

#define DEBOUNCE_MS  50
#define VOL_STEP      5
#define VOL_MIN       0
#define VOL_MAX     100

Adafruit_NeoPixel rgb(1,            RGB_PIN,    NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel ring(RING_LEDS,   RING_PIN,   NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel matrix(MATRIX_LEDS, MATRIX_PIN, NEO_GRB + NEO_KHZ800);

// ── State machine ─────────────────────────────────────────────────────────────
enum State { S_OFF, S_TAG_ON_BURST, S_PLAYING, S_PAUSED, S_TAG_OFF_FADE, S_VOLUME };
State currentState   = S_OFF;
State preVolumeState = S_OFF;

unsigned long stateStart = 0;
unsigned long lastFrame  = 0;

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
#define SOC_REPORT_INTERVAL_MS  30000  // report to Pi every 30s

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
  return raw * BUS_VOLTAGE_LSB;
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
  12.60f,  // 100%
};

float voltageToSoC(float v) {
  if (v <= OCV_V[0])  return 0.0f;
  if (v >= OCV_V[10]) return 100.0f;
  for (int i = 0; i < 10; i++) {
    if (v <= OCV_V[i + 1]) {
      float t = (v - OCV_V[i]) / (OCV_V[i + 1] - OCV_V[i]);
      return (i + t) * 10.0f;
    }
  }
  return 100.0f;
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

  // Rest state detection
  if (abs(packCurrentMA) < PACK_REST_I_MA) {
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

  // Full charge anchor: CV phase detected (high voltage + tapering current)
  if (packVoltage >= PACK_FULL_V - 0.1f && isCharging &&
      abs(packCurrentMA) < CHARGE_TAPER_MA) {
    socPercent = 100.0f;
    Serial.println("CV taper detected -> SoC = 100%");
  }

  // Empty anchor
  if (packVoltage <= PACK_EMPTY_V + 0.1f && !isCharging) {
    socPercent = 0.0f;
  }

  // Report to Pi periodically
  if (now - lastSoCReport >= SOC_REPORT_INTERVAL_MS || !socValid) {
    socValid = true;
    reportSoC();
  }
}

void reportSoC() {
  lastSoCReport = millis();
  String msg = "{\"event\":\"SOC\""
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
  if (isCharging)    { r = 0;   g = 100; b = 200; }
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
  matrixSet(7, 7, r, g, b);
  matrix.show();

  int litLeds = (soc * RING_LEDS) / 100;
  ring.clear();
  for (int i = 0; i < litLeds; i++) {
    float t    = (float)i / RING_LEDS;
    uint8_t lr, lg, lb = 0;
    if (isCharging) { lr = 0; lg = 100; lb = (uint8_t)(200 * t); }
    else {
      lr = t < 0.5 ? (uint8_t)(t * 2 * 80) : 255;
      lg = t < 0.5 ? 255 : (uint8_t)(255 - ((t - 0.5) * 2 * 255));
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

// ── Helpers ───────────────────────────────────────────────────────────────────
void setRGB(uint8_t r, uint8_t g, uint8_t b) {
  rgb.setPixelColor(0, rgb.Color(r, g, b)); rgb.show();
}
void allOff() {
  ring.clear(); ring.show(); matrixClear(); matrix.show(); setRGB(0,0,0);
}

void setState(State s) {
  currentState = s;
  stateStart   = millis();
  lastFrame    = millis();
  if (s == S_OFF)          allOff();
  if (s == S_TAG_OFF_FADE) fadeVal   = 1.0;
  if (s == S_PLAYING)      { spinPos = 0; breathVal = 1.0; breathDir = -1; waveOffset = 0; }
  if (s == S_PAUSED)       { breathVal = 1.0; breathDir = -1; }
}

void overlaySoC() {
  if (!socValid) return;
  drawSoC((int)socPercent);
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
  overlaySoC();
  setRGB(animR/4, animG/4, animB/4);
}

void frameFade() {
  fadeVal -= 0.025;
  if (fadeVal <= 0) { setState(S_OFF); return; }
  ring.fill(ring.Color((uint8_t)(animR*fadeVal),(uint8_t)(animG*fadeVal),(uint8_t)(animB*fadeVal)));
  ring.show();
  drawMatrixSolid((uint8_t)(animR*fadeVal),(uint8_t)(animG*fadeVal),(uint8_t)(animB*fadeVal));
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
}

void frameOff() {
  if (socValid) drawSoC((int)socPercent);
}

// ── Event handlers ────────────────────────────────────────────────────────────
void onReady() {
  ring.fill(ring.Color(180,180,180));
  drawMatrixSolid(180,180,180);
  ring.show(); setRGB(180,180,180);
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
  volumeLevel    = constrain(vol, VOL_MIN, VOL_MAX);
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

void handleEvent(const String& line) {
  Serial.print("Pi: "); Serial.println(line);
  String event = extractValue(line, "event");
  if      (event == "READY" || event == "IDLE") onReady();
  else if (event == "PING") { Serial1.println("{\"event\":\"PONG\"}"); Serial.println("{\"event\":\"PONG\"}"); }
  else if (event == "TAG_ON")   onTagOn(extractValue(line, "mapped") == "true");
  else if (event == "TAG_OFF" || event == "TAG_UNKNOWN") setState(S_TAG_OFF_FADE);
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
    bool raw = digitalRead(buttons[i].pin) == HIGH;
    if (raw != buttons[i].lastRaw) { buttons[i].lastChange = now; buttons[i].lastRaw = raw; }
    if ((now - buttons[i].lastChange) >= DEBOUNCE_MS && raw != buttons[i].state) {
      buttons[i].state = raw;
      sendButtonEvent(buttons[i].name, raw);
      if (raw) handleButtonPress(buttons[i].name);
    }
  }
}

// ── Setup & loop ──────────────────────────────────────────────────────────────
String inputBuffer = "";

void setup() {
  Serial.begin(115200);

  rgb.begin();    rgb.setBrightness(80);
  ring.begin();   ring.setBrightness(60);
  matrix.begin(); matrix.setBrightness(40);
  allOff();
  delay(200);

  pinMode(BTN_PREV,    INPUT_PULLDOWN);
  pinMode(BTN_PLAY,    INPUT_PULLDOWN);
  pinMode(BTN_NEXT,    INPUT_PULLDOWN);
  pinMode(BTN_VOLUP,   INPUT_PULLDOWN);
  pinMode(BTN_VOLDOWN, INPUT_PULLDOWN);

  // Startup rainbow
  uint32_t colors[6] = {
    rgb.Color(200,0,0), rgb.Color(200,100,0), rgb.Color(0,200,0),
    rgb.Color(0,200,200), rgb.Color(0,0,200), rgb.Color(150,0,200)
  };
  for (int i = 0; i < 6; i++) {
    rgb.setPixelColor(0, colors[i]);
    ring.fill(colors[i]);
    for (int p = 0; p < MATRIX_LEDS; p++) matrix.setPixelColor(p, colors[i]);
    rgb.show(); ring.show(); matrix.show();
    delay(120);
  }
  allOff();

  // INA226 init
  ina226_init();
  delay(200);

  if (ina226_scan()) {
    Serial.println("INA226 found at 0x40");
    // Initial voltage-based SoC estimate
    packVoltage = ina226_voltage();
    socPercent  = voltageToSoC(packVoltage);
    socValid    = true;
    Serial.print("Initial SoC from voltage: ");
    Serial.print(socPercent, 1); Serial.println("%");
    reportSoC();
  } else {
    Serial.println("INA226 not found!");
  }

  lastSoCRead   = millis();
  lastSoCReport = millis();
  lastRestStart = millis();

  Serial1.setTX(0); Serial1.setRX(1); Serial1.begin(115200);
}

void loop() {
  // Animation frame ~60fps
  if (millis() - lastFrame >= 16) {
    lastFrame = millis();
    switch (currentState) {
      case S_TAG_ON_BURST: frameBurst();   break;
      case S_PLAYING:      framePlaying(); break;
      case S_PAUSED:       framePaused();  break;
      case S_TAG_OFF_FADE: frameFade();    break;
      case S_VOLUME:       frameVolume();  break;
      case S_OFF:          frameOff();     break;
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
    }
  }
}
