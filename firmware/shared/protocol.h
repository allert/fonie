#ifndef PROTOCOL_H
#define PROTOCOL_H

// Shared constants and event names for Fonie MCU communication

#define EVENT_READY          "READY"
#define EVENT_IDLE           "IDLE"
#define EVENT_TAG_ON         "TAG_ON"
#define EVENT_TAG_OFF        "TAG_OFF"
#define EVENT_TAG_UNKNOWN    "TAG_UNKNOWN"
#define EVENT_PLAYING        "PLAYING"
#define EVENT_PAUSED         "PAUSED"
#define EVENT_VOLUME         "VOLUME"
#define EVENT_BRIGHTNESS     "BRIGHTNESS"
#define EVENT_BUTTON         "BUTTON"
#define EVENT_BUTTON_ACTION  "BUTTON_ACTION"
#define EVENT_SOC            "SOC"
#define EVENT_ERROR          "ERROR"
#define EVENT_SHUTDOWN       "SHUTDOWN"
#define EVENT_ENTER_OTA      "ENTER_OTA"
#define EVENT_WIFI_CONFIG    "WIFI_CONFIG"
#define EVENT_PING           "PING"
#define EVENT_PONG           "PONG"
#define EVENT_BOOTING        "BOOTING"

#endif // PROTOCOL_H
