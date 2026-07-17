#include <SPI.h>
#include <Wire.h>
#include <PN532_I2C.h>
#include <PN532.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <ArduinoOTA.h>
#include <ArduinoJson.h>
#include <LittleFS.h>

#define SDA_PIN 3
#define SCL_PIN 4
#define RPI_TX  21   // ESP32 TX → Pi RX
#define RPI_RX  20   // ESP32 RX ← Pi TX

PN532_I2C pn532_i2c(Wire);
PN532 nfc(pn532_i2c);
HardwareSerial RpiSerial(1);   // UART1

const unsigned long POLL_INTERVAL_MS   = 50;
const unsigned long REMOVAL_TIMEOUT_MS = 1000;

bool inApMode = false;
bool inStaMode = false;
bool pn532_connected = false;
bool tagPresent = false;
String currentUid = "";
unsigned long lastSeen = 0, lastPoll = 0;

// Wi-Fi / Captive Portal State
const byte DNS_PORT = 53;
DNSServer dnsServer;
WebServer server(80);
String uidToString(uint8_t *uid, uint8_t len) {
  String s;
  for (uint8_t i = 0; i < len; i++) { if (uid[i] < 0x10) s += "0"; s += String(uid[i], HEX); }
  s.toUpperCase(); return s;
}

// -- Captive Portal Handlers --
void handleRoot() {
  if (LittleFS.exists("/index.html")) {
    File file = LittleFS.open("/index.html", "r");
    server.streamFile(file, "text/html");
    file.close();
  } else {
    server.send(500, "text/plain", "Error: index.html not found in LittleFS. Did you upload the File System Image?");
  }
}

void handleScan() {
  int n = WiFi.scanNetworks();
  JsonDocument doc;
  JsonArray array = doc.to<JsonArray>();
  for (int i = 0; i < n; ++i) {
    JsonObject obj = array.add<JsonObject>();
    obj["ssid"] = WiFi.SSID(i);
    obj["rssi"] = WiFi.RSSI(i);
    obj["secure"] = WiFi.encryptionType(i) != WIFI_AUTH_OPEN;
  }
  String json;
  serializeJson(doc, json);
  server.send(200, "application/json", json);
}

void handleSave() {
  String ssid = server.arg("ssid");
  String pass = server.arg("pass");
  
  // Send back to Pi
  JsonDocument doc;
  doc["event"] = "WIFI_CONFIG";
  doc["ssid"] = ssid;
  doc["pass"] = pass;
  String out;
  serializeJson(doc, out);
  
  // Flush UART and delay slightly to ensure transmission isn't corrupted
  RpiSerial.println(out);
  RpiSerial.flush();
  delay(10);
  
  Serial.println("Sent WIFI_CONFIG to Pi: " + ssid);

  server.send(200, "application/json", "{\"status\":\"ok\"}");
}

void startAP() {
  if (inApMode) return;
  
  Serial.println("Starting Captive Portal AP: Fonie-Setup");
  
  // Robust Wi-Fi AP Initialization for ESP32-C3
  WiFi.disconnect(true, true);
  delay(100);
  WiFi.mode(WIFI_AP);
  
  // Workaround for some C3 boards browning out or failing RF calibration
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
  
  // Start AP on channel 1, no password, max 4 connections
  bool success = WiFi.softAP("Fonie-Setup", NULL, 1, 0, 4);
  
  if (success) {
    Serial.print("AP started successfully! IP Address: ");
    Serial.println(WiFi.softAPIP());
  } else {
    Serial.println("FAILED to start AP!");
  }
  
  dnsServer.start(DNS_PORT, "*", WiFi.softAPIP());
  
  server.on("/", HTTP_GET, handleRoot);
  server.on("/scan", HTTP_GET, handleScan);
  server.on("/save", HTTP_POST, handleSave);
  server.onNotFound(handleRoot); // Redirect all to root
  server.begin();
  
  inApMode = true;
  inStaMode = false;
}

void startSTA(const char* ssid, const char* pass) {
  if (inApMode) {
    dnsServer.stop();
    server.stop();
    inApMode = false;
  }
  Serial.printf("Connecting to Wi-Fi STA: %s\n", ssid);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("fonie-esp32");
  WiFi.begin(ssid, pass);
  
  ArduinoOTA.setHostname("fonie-esp32");
  ArduinoOTA.begin();
  inStaMode = true;
}

void setup() {
  Serial.begin(115200);                                    // USB debug
  unsigned long startWait = millis();
  while (!Serial && millis() - startWait < 4000) {
    delay(10); // Wait up to 4s for serial monitor to connect
  }
  Serial.println("\n\n--- Fonie ESP32 Booting ---");
  RpiSerial.begin(115200, SERIAL_8N1, RPI_RX, RPI_TX);   // UART to Pi

  if (!LittleFS.begin(true)) {
    Serial.println("LittleFS Mount Failed");
  } else {
    Serial.println("LittleFS Mounted Successfully");
  }

  Wire.begin(SDA_PIN, SCL_PIN);
  nfc.begin();
  if (!nfc.getFirmwareVersion()) {
    Serial.println("No PN532 found. Running in UART-only mode.");
    RpiSerial.println("{\"event\":\"ERROR\",\"msg\":\"No PN532 found\"}");
    pn532_connected = false;
  } else {
    nfc.SAMConfig();
    Serial.println("PN532 ready");
    pn532_connected = true;
  }
  RpiSerial.println("{\"event\":\"READY\"}");
  Serial.println("ESP32 ready and listening");
}

// Global input buffer for UART from Pi
String inputBuffer = "";

void loop() {
  static unsigned long lastHeartbeat = 0;
  if (millis() - lastHeartbeat > 5000) {
    // Uncomment this to verify the board is still running!
    // Serial.println("ESP32 Heartbeat... Still listening!");
    lastHeartbeat = millis();
  }

  if (inApMode) {
    dnsServer.processNextRequest();
    server.handleClient();
  }
  if (inStaMode && WiFi.status() == WL_CONNECTED) {
    ArduinoOTA.handle();
  }

  // Read UART from Pi (RpiSerial) or USB (Serial) for debugging
  while (RpiSerial.available() || Serial.available()) {
    char c;
    if (Serial.available()) {
      c = (char)Serial.read();
    } else {
      c = (char)RpiSerial.read();
    }

    if (c == '\n') {
      inputBuffer.trim();
      if (inputBuffer.length() > 0) {
        if (inputBuffer.indexOf("\"event\":\"PING\"") >= 0 || inputBuffer.indexOf("\"event\": \"PING\"") >= 0) {
          Serial.println("Received PING");
          RpiSerial.println("{\"event\":\"PONG\"}");
          Serial.println("{\"event\":\"PONG\"}");
        } 
        else if (inputBuffer.indexOf("WIFI_AP_START") >= 0) {
          Serial.println("Received WIFI_AP_START, attempting startAP()...");
          RpiSerial.println("{\"event\":\"DEBUG\",\"msg\":\"ESP32: Received WIFI_AP_START\"}");
          startAP();
          RpiSerial.println("{\"event\":\"DEBUG\",\"msg\":\"ESP32: startAP() finished\"}");
        }
        else if (inputBuffer.indexOf("WIFI_CONNECT") >= 0) {
          Serial.println("Received WIFI_CONNECT");
          JsonDocument doc;
          DeserializationError error = deserializeJson(doc, inputBuffer);
          if (!error) {
            const char* ssid = doc["ssid"] | "";
            const char* pass = doc["pass"] | "";
            startSTA(ssid, pass);
          } else {
            Serial.println("Failed to parse WIFI_CONNECT JSON");
          }
        }
      }
      inputBuffer = "";
    } else {
      inputBuffer += c;
    }
  }

  unsigned long now = millis();
  if (pn532_connected && (now - lastPoll >= POLL_INTERVAL_MS)) {
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
}
