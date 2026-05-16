#include <Wire.h>
#include <PN532_I2C.h>
#include <PN532.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <ArduinoOTA.h>
#include <ArduinoJson.h>

#define SDA_PIN 3
#define SCL_PIN 4
#define RPI_TX  21   // ESP32 TX → Pi RX
#define RPI_RX  20   // ESP32 RX ← Pi TX

PN532_I2C pn532_i2c(Wire);
PN532 nfc(pn532_i2c);
HardwareSerial RpiSerial(1);   // UART1

const unsigned long POLL_INTERVAL_MS   = 50;
const unsigned long REMOVAL_TIMEOUT_MS = 300;

bool tagPresent = false;
String currentUid = "";
unsigned long lastSeen = 0, lastPoll = 0;

// Wi-Fi / Captive Portal State
const byte DNS_PORT = 53;
DNSServer dnsServer;
WebServer server(80);
bool inApMode = false;
bool inStaMode = false;

String uidToString(uint8_t *uid, uint8_t len) {
  String s;
  for (uint8_t i = 0; i < len; i++) { if (uid[i] < 0x10) s += "0"; s += String(uid[i], HEX); }
  s.toUpperCase(); return s;
}

// -- Captive Portal Handlers --
void handleRoot() {
  String html = "<html><head><meta name='viewport' content='width=device-width, initial-scale=1'><style>"
                "body{font-family:sans-serif;padding:20px;background:#f4f4f9;color:#333;}"
                "input{display:block;width:100%;padding:10px;margin-bottom:15px;border:1px solid #ccc;border-radius:4px;}"
                "button{background:#007bff;color:white;border:none;padding:12px;width:100%;border-radius:4px;font-size:16px;}"
                "</style></head><body>"
                "<h2>Fonie Wi-Fi Setup</h2>"
                "<form action='/save' method='POST'>"
                "<label>Network Name (SSID)</label><input type='text' name='ssid' required>"
                "<label>Password</label><input type='password' name='pass'>"
                "<button type='submit'>Connect Fonie</button>"
                "</form></body></html>";
  server.send(200, "text/html", html);
}

void handleSave() {
  String ssid = server.arg("ssid");
  String pass = server.arg("pass");
  // Send back to Pi
  StaticJsonDocument<200> doc;
  doc["event"] = "WIFI_CONFIG";
  doc["ssid"] = ssid;
  doc["pass"] = pass;
  String out;
  serializeJson(doc, out);
  RpiSerial.println(out);
  Serial.println("Sent WIFI_CONFIG to Pi: " + ssid);

  server.send(200, "text/html", "<html><head><meta name='viewport' content='width=device-width, initial-scale=1'></head>"
                                "<body style='font-family:sans-serif;padding:20px;text-align:center;'>"
                                "<h2>Credentials sent!</h2><p>Fonie is connecting to <b>" + ssid + "</b>.</p></body></html>");
}

void startAP() {
  if (inApMode) return;
  if (inStaMode) {
    WiFi.disconnect();
    inStaMode = false;
  }
  Serial.println("Starting Captive Portal AP: Fonie-Setup");
  WiFi.mode(WIFI_AP);
  WiFi.softAP("Fonie-Setup");
  dnsServer.start(DNS_PORT, "*", WiFi.softAPIP());
  
  server.on("/", handleRoot);
  server.on("/save", handleSave);
  server.onNotFound(handleRoot); // Redirect all to root
  server.begin();
  
  inApMode = true;
}

void startSTA(const char* ssid, const char* pass) {
  if (inApMode) {
    dnsServer.stop();
    server.stop();
    inApMode = false;
  }
  Serial.printf("Connecting to Wi-Fi STA: %s\n", ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass);
  
  // Setup OTA
  ArduinoOTA.setHostname("fonie-esp32");
  ArduinoOTA.begin();
  inStaMode = true;
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
  if (inApMode) {
    dnsServer.processNextRequest();
    server.handleClient();
  }
  if (inStaMode && WiFi.status() == WL_CONNECTED) {
    ArduinoOTA.handle();
  }

  // Read UART from Pi
  while (RpiSerial.available()) {
    char c = (char)RpiSerial.read();
    if (c == '\n') {
      inputBuffer.trim();
      if (inputBuffer.length() > 0) {
        if (inputBuffer.indexOf("\"event\":\"PING\"") >= 0 || inputBuffer.indexOf("\"event\": \"PING\"") >= 0) {
          RpiSerial.println("{\"event\":\"PONG\"}");
        } 
        else if (inputBuffer.indexOf("WIFI_AP_START") >= 0) {
          startAP();
        }
        else if (inputBuffer.indexOf("WIFI_CONNECT") >= 0) {
          StaticJsonDocument<256> doc;
          DeserializationError error = deserializeJson(doc, inputBuffer);
          if (!error) {
            const char* ssid = doc["ssid"] | "";
            const char* pass = doc["pass"] | "";
            startSTA(ssid, pass);
          }
        }
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
