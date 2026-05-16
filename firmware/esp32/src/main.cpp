#include <Wire.h>
#include <PN532_I2C.h>
#include <PN532.h>

#define SDA_PIN 3
#define SCL_PIN 4
#define RPI_TX  21   // ESP32 TX → Pi RX
#define RPI_RX  20   // ESP32 RX ← Pi TX

PN532_I2C pn532_i2c(Wire);
PN532 nfc(pn532_i2c);
HardwareSerial RpiSerial(1);   // UART1 — C3 only has UART0 and UART1 exposed

const unsigned long POLL_INTERVAL_MS   = 50;
const unsigned long REMOVAL_TIMEOUT_MS = 300;

bool tagPresent = false;
String currentUid = "";
unsigned long lastSeen = 0, lastPoll = 0;

String uidToString(uint8_t *uid, uint8_t len) {
  String s;
  for (uint8_t i = 0; i < len; i++) { if (uid[i] < 0x10) s += "0"; s += String(uid[i], HEX); }
  s.toUpperCase(); return s;
}

void setup() {
  Serial.begin(115200);                                    // USB debug
  RpiSerial.begin(115200, SERIAL_8N1, RPI_RX, RPI_TX);   // UART to Pi

  Wire.begin(SDA_PIN, SCL_PIN);
  nfc.begin();
  if (!nfc.getFirmwareVersion()) {
    Serial.println("No PN532 found");
    RpiSerial.println("{\"event\":\"ERROR\",\"msg\":\"No PN532 found\"}");
    while (1);
  }
  nfc.SAMConfig();
  Serial.println("PN532 ready");
  RpiSerial.println("{\"event\":\"READY\"}");
}

// Global input buffer for UART from Pi
String inputBuffer = "";

void loop() {
  // Read UART from Pi
  while (RpiSerial.available()) {
    char c = (char)RpiSerial.read();
    if (c == '\n') {
      inputBuffer.trim();
      if (inputBuffer.indexOf("\"event\":\"PING\"") >= 0 || inputBuffer.indexOf("\"event\": \"PING\"") >= 0) {
        RpiSerial.println("{\"event\":\"PONG\"}");
      }
      inputBuffer = "";
    } else {
      inputBuffer += c;
    }
  }

  unsigned long now = millis();
  if (now - lastPoll < POLL_INTERVAL_MS) return;
  lastPoll = now;

  uint8_t uid[7], uidLength;
  bool ok = nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLength, 5);

  if (ok) {
    String uidStr = uidToString(uid, uidLength);
    lastSeen = now;
    if (!tagPresent) {
      tagPresent = true;
      currentUid = uidStr;
      RpiSerial.printf("{\"event\":\"TAG_ON\",\"uid\":\"%s\"}\n", currentUid.c_str());
      Serial.printf("TAG_ON %s\n", currentUid.c_str());
    } else if (uidStr != currentUid) {
      RpiSerial.printf("{\"event\":\"TAG_OFF\",\"uid\":\"%s\"}\n", currentUid.c_str());
      RpiSerial.printf("{\"event\":\"TAG_ON\",\"uid\":\"%s\"}\n", uidStr.c_str());
      currentUid = uidStr;
      Serial.printf("TAG_CHANGED %s\n", currentUid.c_str());
    }
  } else if (tagPresent && (now - lastSeen) > REMOVAL_TIMEOUT_MS) {
    RpiSerial.printf("{\"event\":\"TAG_OFF\",\"uid\":\"%s\"}\n", currentUid.c_str());
    Serial.printf("TAG_OFF %s\n", currentUid.c_str());
    tagPresent = false;
    currentUid = "";
  }
}
