/**
 * @file ui_status_bar.h
 * Status Bar Header (Top Right-Aligned) for Portrait 240x320 HMI
 */

#ifndef UI_STATUS_BAR_H
#define UI_STATUS_BAR_H

#include <stdint.h>
#include <stdbool.h>
#include "lvgl.h"
#include "ui_theme.h"
#include "ui_icons.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Icon State Models
 *
 * The camera icon is always drawn and recoloured to show idle/active. The mic
 * and speaker icons instead report a single exception -- "this input/output is
 * muted" -- and are hidden outright the rest of the time, so an unremarkable
 * status bar carries only the battery. The physical mute switches are the
 * source of these states; see buttons.h. */
typedef enum {
    CAM_OFF = 0,
    CAM_ACTIVE
} cam_state_t;

typedef enum {
    MIC_UNMUTED = 0,   /* icon hidden */
    MIC_MUTED          /* icon shown  */
} mic_state_t;

typedef enum {
    SPEAKER_UNMUTED = 0,   /* icon hidden */
    SPEAKER_MUTED          /* icon shown  */
} speaker_state_t;

/**
 * @brief Initialize and render the status bar at top of screen.
 * @param parent Parent container object (or active screen).
 * @return Pointer to created status bar container object.
 */
lv_obj_t* ui_status_bar_create(lv_obj_t *parent);

/**
 * @brief Apply Camera state (idle/active recolor). Top-left icon, no animation.
 * @param state CAM_OFF or CAM_ACTIVE.
 */
void ui_cam_set_state(cam_state_t state);

/**
 * @brief Show/hide the Mic icon. Hidden unless the mic is muted.
 * @param state MIC_UNMUTED (hidden) or MIC_MUTED (shown).
 */
void ui_mic_set_state(mic_state_t state);

/**
 * @brief Set the speaker mute flag.
 *
 * The icon appears only when the speaker is BOTH muted and in use -- see
 * ui_speaker_set_in_use(). Setting this alone will not reveal it.
 *
 * @param state SPEAKER_UNMUTED or SPEAKER_MUTED.
 */
void ui_speaker_set_state(speaker_state_t state);

/**
 * @brief Declare whether the current screen actually uses the speaker.
 *
 * The speaker only plays on RESULT (that is where the translation is spoken),
 * so a "speaker muted" warning is meaningless anywhere else and the icon stays
 * hidden regardless of the mute flag. Driven by ui_set_state(), the same way
 * the camera is -- callers do not normally invoke this directly.
 *
 * @param in_use true on RESULT, false on every other state.
 */
void ui_speaker_set_in_use(bool in_use);

/**
 * @brief Set the "text-to-speech is currently playing" flag.
 *
 * ENTRY POINT ONLY -- nothing calls this yet. No TTS engine exists in this
 * codebase; this is the hook a future integration should call with true just
 * before playback starts and false when it ends.
 *
 * Fully independent of the mute flag; both may be true at once. When they are,
 * muted wins visually, because nothing is audible through a muted speaker and
 * showing a "playing" indicator would be a lie.
 *
 * Takes effect only on RESULT -- see ui_speaker_set_in_use().
 *
 * @param playing true while TTS audio is playing.
 */
void ui_speaker_set_tts_playing(bool playing);

/**
 * @brief Current "TTS is playing" flag.
 */
bool ui_speaker_tts_is_playing(void);

/**
 * @brief Apply Battery fill percentage (0..100) and color band.
 * @param percent Fill level 0..100.
 */
void ui_battery_set_value(uint8_t percent);

#ifdef __cplusplus
}
#endif

#endif /* UI_STATUS_BAR_H */
