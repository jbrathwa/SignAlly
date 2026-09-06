/**
 * @file ui_center.h
 * Center Dynamic Section (4-State Machine) Header for Portrait 240x320 HMI
 */

#ifndef UI_CENTER_H
#define UI_CENTER_H

#include <stdint.h>
#include <stdbool.h>
#include "lvgl.h"
#include "ui_theme.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 6-State Machine
 *
 *   (power on) --> BOOT --(auto, ~1.5s)--> INTRO --(Start/Stop button)--> LISTENING
 *   LISTENING --(input captured)--> ANALYZING --> RESULT
 *   LISTENING/ANALYZING --(failure)--> ERROR
 *   RESULT --(Start/Stop button)--> LISTENING
 *   ERROR  --(Start/Stop button)--> LISTENING
 *
 * BOOT and INTRO are power-on-only: each is entered exactly once per power
 * cycle (BOOT from ui_center_create(), INTRO only from BOOT's own auto-advance
 * timer) and no other code path may ever target them -- once the unit leaves
 * INTRO via the physical Start/Stop button it never returns to BOOT or INTRO
 * without a reboot. The center section carries no on-screen controls at all
 * -- see buttons.h for the input layer that drives it.
 *
 * LISTENING and ANALYZING share a single container so the two can never drift
 * apart. They differ only in label text and indicator diameter (LISTENING is
 * the smaller circle), which is now the sole visual cue between them.
 */
typedef enum {
    UI_STATE_BOOT = 0,
    UI_STATE_INTRO,
    UI_STATE_LISTENING,
    UI_STATE_ANALYZING,
    UI_STATE_RESULT,
    UI_STATE_ERROR
} ui_state_t;

/**
 * @brief Initialize and create the center dynamic section objects.
 * @param parent Parent container object.
 * @return Pointer to created center section container object.
 */
lv_obj_t* ui_center_create(lv_obj_t *parent);

/**
 * @brief Central State Transition Function.
 * Hides/shows objects based on active state and drives the camera icon
 * (active during LISTENING and ANALYZING, off otherwise).
 * @param state Target state.
 */
void ui_set_state(ui_state_t state);

/**
 * @brief Set dynamic result text for RESULT state.
 * New text simply overwrites the previous text; no history is kept.
 * @param text Result string to display.
 */
void ui_show_result(const char *text);

/**
 * @brief Wipe the result label so no stale text can flash on the next RESULT.
 * Called by the shared Start/Stop handler before it returns to LISTENING.
 */
void ui_clear_result(void);

/**
 * @brief Set the sentence shown on the ERROR screen.
 *
 * The orchestrator's `error` messages carry real operator-facing text -- "Move
 * back", "Nobody in frame", "Recognition offline", "Camera not found" -- and
 * each says something different about what to do next. Painting all four as a
 * single fixed "Something went wrong" throws that away.
 *
 * @param text Message to show. NULL or "" restores the generic default, which
 *             is what a caller with nothing specific to say should pass.
 */
void ui_show_error(const char *text);

/**
 * @brief Get current active UI state.
 * @return ui_state_t Active state.
 */
ui_state_t ui_get_state(void);

#ifdef __cplusplus
}
#endif

#endif /* UI_CENTER_H */
