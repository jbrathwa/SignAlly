/**
 * @file demo_manager.cpp
 * LittleFS JSON Demo Manager Implementation for Elecrow CrowPanel 2.8" HMI
 */

#include "demo_manager.h"
#include <LittleFS.h>
#include <ArduinoJson.h>
#include "esp_log.h"

static const char *TAG = "demo_manager";
static bool littlefs_mounted = false;

static bool parse_and_apply_json(const String &json_data)
{
    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, json_data);

    if (error) {
        ESP_LOGE(TAG, "JSON Deserialization failed: %s", error.c_str());
        return false;
    }

    /* 1. Parse Status Bar Icons & Battery */
    if (doc.containsKey("battery")) {
        uint8_t bat = doc["battery"].as<uint8_t>();
        ui_battery_set_value(bat);
    }

    if (doc.containsKey("cam_active")) {
        bool cam = doc["cam_active"].as<bool>();
        ui_cam_set_state(cam ? CAM_ACTIVE : CAM_OFF);
    }

    /*
     * "mic_active"/"speaker_active" mean the device is live, i.e. NOT muted --
     * so the icons, which now appear only to flag a mute, show on `false`.
     * Routed through the buttons layer so the JSON demo state and the physical
     * mute button share one flag instead of drifting apart.
     */
    if (doc.containsKey("mic_active")) {
        bool mic = doc["mic_active"].as<bool>();
        buttons_set_mic_muted(!mic);
    }

    if (doc.containsKey("speaker_active")) {
        bool spk = doc["speaker_active"].as<bool>();
        buttons_set_speaker_muted(!spk);
    }

    /* 2. Parse Result Text */
    if (doc.containsKey("result_text")) {
        const char *res_text = doc["result_text"].as<const char*>();
        if (res_text != NULL) {
            ui_show_result(res_text);
        }
    }

    /* 3. Parse and Apply Center UI State Machine */
    if (doc.containsKey("state")) {
        const char *state_str = doc["state"].as<const char*>();
        if (state_str != NULL) {
            if (strcmp(state_str, "LISTENING") == 0) {
                ui_set_state(UI_STATE_LISTENING);
            } else if (strcmp(state_str, "ANALYZING") == 0) {
                ui_set_state(UI_STATE_ANALYZING);
            } else if (strcmp(state_str, "RESULT") == 0) {
                ui_set_state(UI_STATE_RESULT);
            } else if (strcmp(state_str, "ERROR") == 0) {
                ui_set_state(UI_STATE_ERROR);
            } else {
                ESP_LOGW(TAG, "Unknown state in JSON: %s", state_str);
            }
        }
    }

    ESP_LOGI(TAG, "Successfully parsed and applied JSON state to UI.");
    return true;
}

bool demo_manager_reload(void)
{
    if (!littlefs_mounted) {
        ESP_LOGE(TAG, "LittleFS is not mounted!");
        return false;
    }

    if (!LittleFS.exists("/demo_state.json")) {
        ESP_LOGE(TAG, "File /demo_state.json not found on LittleFS!");
        return false;
    }

    File file = LittleFS.open("/demo_state.json", "r");
    if (!file) {
        ESP_LOGE(TAG, "Failed to open /demo_state.json for reading!");
        return false;
    }

    String content = file.readString();
    file.close();

    ESP_LOGI(TAG, "Read %d bytes from /demo_state.json", content.length());
    return parse_and_apply_json(content);
}

bool demo_manager_init(void)
{
    ESP_LOGI(TAG, "Mounting LittleFS filesystem...");

    if (!LittleFS.begin(true)) { /* Format on fail */
        ESP_LOGE(TAG, "Failed to mount LittleFS!");
        littlefs_mounted = false;
        return false;
    }

    littlefs_mounted = true;

    /* Mount only. Applying the file here is what used to make a panel with a
     * dead UART link look alive -- it came up with a plausible battery level
     * and mute icons that no MCU had ever sent. The status bar now stays at its
     * built-in defaults until a {"t":"status"} message arrives, so a silent
     * link is visible on the screen instead of hidden by demo data. */
    ESP_LOGI(TAG, "LittleFS mounted. demo_state.json NOT applied -- type "
                  "'reload' to force it onto the UI.");
    return true;
}

void demo_manager_check_serial(void)
{
    if (Serial.available() > 0) {
        String input = Serial.readStringUntil('\n');
        input.trim();

        if (input.equalsIgnoreCase("reload")) {
            ESP_LOGI(TAG, "Serial 'reload' command received! Re-reading /demo_state.json...");
            if (demo_manager_reload()) {
                ESP_LOGI(TAG, "UI reloaded live from LittleFS successfully.");
            } else {
                ESP_LOGE(TAG, "Failed to reload UI from LittleFS.");
            }
        }
    }
}
