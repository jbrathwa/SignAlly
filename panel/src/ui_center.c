/**
 * @file ui_center.c
 * Center Dynamic Section (6-screen state machine) Implementation for Portrait 240x320 HMI
 * Screens and the flows between them: docs/panel.md section 3.
 */

#include "ui_center.h"
#include "ui_status_bar.h"
#include <stdio.h>
#include "esp_log.h"

static const char *TAG = "ui_center";

/* Active State Tracker */
static ui_state_t current_ui_state = UI_STATE_BOOT;

/* Main & Sub-Containers.
 * LISTENING and ANALYZING deliberately share `active_cont`: the two differ
 * only in label text and indicator diameter, and reusing the one container
 * makes that structural rather than a convention to maintain. */
static lv_obj_t *center_main_cont = NULL;
static lv_obj_t *boot_cont = NULL;
static lv_obj_t *intro_cont = NULL;
static lv_obj_t *active_cont = NULL;
static lv_obj_t *result_cont = NULL;
static lv_obj_t *error_cont = NULL;

/* The ERROR screen's sentence. Held as a handle rather than written once at
 * create time because the orchestrator sends the text with the error -- see
 * ui_show_error(). */
static lv_obj_t *error_label = NULL;
#define UI_ERROR_DEFAULT_TEXT "Something went wrong"

/* BOOT auto-advances to INTRO on its own after a short splash -- no button
 * involved. The timer is torn down unconditionally at the top of
 * ui_set_state() (see below) so a stale one can never fire after the state
 * has already moved on. */
static lv_timer_t *boot_advance_timer = NULL;
#define UI_BOOT_ADVANCE_MS 1500

/* Widget Handles */
static lv_obj_t *active_arc = NULL;
static lv_obj_t *active_label = NULL;
static lv_obj_t *result_label = NULL;

/* Animation Handles */
static lv_anim_t listening_anim;

/*
 * Indicator diameters. With the on-screen Stop button gone, size is the only
 * thing separating LISTENING from ANALYZING, so the gap has to be large enough
 * to read at a glance: ANALYZING is the larger ring.
 *
 * LISTENING's label is two lines ("Detecting." / "Sign Now"), which is why
 * its ring is much bigger than ANALYZING's single-line "Translating" strictly
 * needs -- the extra diameter is vertical room for the second line, not just
 * width. Both rings comfortably clear their label at montserrat_14; do not
 * shrink either without re-checking the label still fits.
 */
#define INDICATOR_SZ_LISTENING  130
#define INDICATOR_SZ_ANALYZING  154

/**
 * Animation Callback for the pulsing indicator arc
 */
static void anim_listening_pulse_cb(void *var, int32_t val)
{
    lv_arc_set_angles((lv_obj_t*)var, 0, val);
}

/**
 * Start the pulsing indicator sweep (shared by LISTENING and ANALYZING)
 */
static void indicator_pulse_start(void)
{
    if (active_arc == NULL) return;

    lv_anim_init(&listening_anim);
    lv_anim_set_var(&listening_anim, active_arc);
    lv_anim_set_values(&listening_anim, 20, 340);
    lv_anim_set_time(&listening_anim, 1200);
    lv_anim_set_playback_time(&listening_anim, 1200);
    lv_anim_set_repeat_count(&listening_anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_exec_cb(&listening_anim, anim_listening_pulse_cb);
    lv_anim_start(&listening_anim);
}

/**
 * Create a flat, chrome-free state container filling the center content area.
 */
static lv_obj_t* make_state_cont(lv_obj_t *parent)
{
    lv_obj_t *cont = lv_obj_create(parent);
    lv_obj_set_size(cont, 216, 260);
    lv_obj_center(cont);
    lv_obj_set_style_bg_opa(cont, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_border_width(cont, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(cont, 0, LV_PART_MAIN);
    lv_obj_clear_flag(cont, LV_OBJ_FLAG_SCROLLABLE);
    return cont;
}

/*
 * BOOT's auto-advance timer callback. Must null boot_advance_timer BEFORE
 * calling ui_set_state(): this is a repeat-count-1 timer, so LVGL is already
 * in the process of auto-deleting it when the callback runs, and
 * ui_set_state()'s own unconditional teardown (below) would otherwise call
 * lv_timer_del() on a timer LVGL is mid-delete on -- a use-after-free.
 */
static void boot_advance_timer_cb(lv_timer_t *timer)
{
    (void)timer;
    boot_advance_timer = NULL;
    ui_set_state(UI_STATE_INTRO);
}

void ui_set_state(ui_state_t state)
{
    current_ui_state = state;

    if (center_main_cont == NULL) return;

    /* 0. A stale BOOT->INTRO timer must never survive a transition away from
     * BOOT for any reason, so this runs unconditionally before anything else. */
    if (boot_advance_timer != NULL) {
        lv_timer_del(boot_advance_timer);
        boot_advance_timer = NULL;
    }

    /* 1. Hide every state container */
    lv_obj_add_flag(boot_cont, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(intro_cont, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(active_cont, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(result_cont, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(error_cont, LV_OBJ_FLAG_HIDDEN);

    /* 2. Stop the pulse; the active states restart it below */
    if (active_arc != NULL) {
        lv_anim_del(active_arc, anim_listening_pulse_cb);
    }

    /* 3. The speaker only plays on RESULT, so a "speaker muted" warning is
     *    meaningless anywhere else -- the status bar gates the icon on this
     *    as well as on the mute flag. Mic is deliberately not gated: it is
     *    live throughout, so a muted mic matters on every screen. */
    ui_speaker_set_in_use(state == UI_STATE_RESULT);

    /* 4. Reveal the target state and drive the camera icon */
    switch (state) {
        case UI_STATE_BOOT:
            lv_obj_clear_flag(boot_cont, LV_OBJ_FLAG_HIDDEN);
            ui_cam_set_state(CAM_OFF);
            boot_advance_timer = lv_timer_create(boot_advance_timer_cb, UI_BOOT_ADVANCE_MS, NULL);
            lv_timer_set_repeat_count(boot_advance_timer, 1);
            ESP_LOGI(TAG, "UI State -> BOOT");
            break;

        case UI_STATE_INTRO:
            lv_obj_clear_flag(intro_cont, LV_OBJ_FLAG_HIDDEN);
            ui_cam_set_state(CAM_OFF);
            ESP_LOGI(TAG, "UI State -> INTRO");
            break;

        case UI_STATE_LISTENING:
        case UI_STATE_ANALYZING: {
            const bool listening = (state == UI_STATE_LISTENING);
            const lv_coord_t sz = listening ? INDICATOR_SZ_LISTENING
                                            : INDICATOR_SZ_ANALYZING;

            /* Resizing keeps the stored CENTER alignment, and the label is
             * aligned to the container rather than the arc, so the text stays
             * centred inside the ring at either diameter. */
            lv_obj_set_size(active_arc, sz, sz);
            lv_label_set_text(active_label, listening ? "Detecting.\nSign Now" : "Translating");
            lv_obj_clear_flag(active_cont, LV_OBJ_FLAG_HIDDEN);
            indicator_pulse_start();
            ui_cam_set_state(CAM_ACTIVE);
            ESP_LOGI(TAG, "UI State -> %s", listening ? "LISTENING" : "ANALYZING");
            break;
        }

        case UI_STATE_RESULT:
            lv_obj_clear_flag(result_cont, LV_OBJ_FLAG_HIDDEN);
            ui_cam_set_state(CAM_OFF);
            ESP_LOGI(TAG, "UI State -> RESULT");
            break;

        case UI_STATE_ERROR:
            lv_obj_clear_flag(error_cont, LV_OBJ_FLAG_HIDDEN);
            ui_cam_set_state(CAM_OFF);
            ESP_LOGI(TAG, "UI State -> ERROR");
            break;
    }
}

void ui_show_result(const char *text)
{
    if (result_label != NULL && text != NULL) {
        lv_label_set_text(result_label, text);
    }
}

void ui_clear_result(void)
{
    if (result_label != NULL) {
        lv_label_set_text(result_label, "");
    }
}

void ui_show_error(const char *text)
{
    if (error_label == NULL) return;

    /* An empty string is treated as "nothing specific to say" rather than
     * rendered as a blank screen under a warning triangle, which would read as
     * a UI fault rather than a message. */
    if (text == NULL || text[0] == '\0') {
        lv_label_set_text(error_label, UI_ERROR_DEFAULT_TEXT);
        return;
    }
    lv_label_set_text(error_label, text);
}

ui_state_t ui_get_state(void)
{
    return current_ui_state;
}

lv_obj_t* ui_center_create(lv_obj_t *parent)
{
    /* Main Center Container (240x284 Below Status Bar) */
    center_main_cont = lv_obj_create(parent);
    lv_obj_set_size(center_main_cont, 240, 284);
    lv_obj_align(center_main_cont, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_set_style_bg_color(center_main_cont, UI_COLOR_BG, LV_PART_MAIN);
    lv_obj_set_style_border_width(center_main_cont, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(center_main_cont, 12, LV_PART_MAIN);
    lv_obj_clear_flag(center_main_cont, LV_OBJ_FLAG_SCROLLABLE);

    /* ----------------------------------------------------
     * 0a. STATE: BOOT -- one-time splash, no controls
     * ---------------------------------------------------- */
    boot_cont = make_state_cont(center_main_cont);

    lv_obj_t *boot_label = lv_label_create(boot_cont);
    lv_label_set_text(boot_label, "SignAlly");
    lv_obj_set_style_text_font(boot_label, &lv_font_montserrat_24, LV_PART_MAIN);
    lv_obj_set_style_text_color(boot_label, UI_COLOR_INDICATOR, LV_PART_MAIN);
    lv_obj_align(boot_label, LV_ALIGN_CENTER, 0, 0);

    /* ----------------------------------------------------
     * 0b. STATE: INTRO -- one-time instructions legend, no controls
     * ---------------------------------------------------- */
    intro_cont = make_state_cont(center_main_cont);

    lv_obj_t *intro_title = lv_label_create(intro_cont);
    lv_label_set_text(intro_title, "SignAlly");
    lv_obj_set_style_text_font(intro_title, &lv_font_montserrat_24, LV_PART_MAIN);
    lv_obj_set_style_text_color(intro_title, UI_COLOR_INDICATOR, LV_PART_MAIN);
    lv_obj_align(intro_title, LV_ALIGN_TOP_MID, 0, 6);

    lv_obj_t *intro_start_stop = lv_label_create(intro_cont);
    lv_label_set_text(intro_start_stop, "Press " LV_SYMBOL_DOWN " to Start / Stop");
    lv_obj_set_style_text_font(intro_start_stop, &lv_font_montserrat_14, LV_PART_MAIN);
    lv_obj_set_style_text_color(intro_start_stop, UI_COLOR_TEXT_RESULT, LV_PART_MAIN);
    lv_obj_set_style_text_align(intro_start_stop, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_align(intro_start_stop, LV_ALIGN_CENTER, 0, 0);

    lv_obj_t *intro_hint = lv_label_create(intro_cont);
    lv_label_set_text(intro_hint, "Tap Start to begin");
    lv_obj_set_style_text_font(intro_hint, &lv_font_montserrat_14, LV_PART_MAIN);
    lv_obj_set_style_text_color(intro_hint, UI_COLOR_ICON_IDLE, LV_PART_MAIN);
    lv_obj_align(intro_hint, LV_ALIGN_BOTTOM_MID, 0, -6);

    /* ----------------------------------------------------
     * 1. STATE: LISTENING / ANALYZING -- shared pulsing indicator, no controls
     * ---------------------------------------------------- */
    active_cont = make_state_cont(center_main_cont);

    active_arc = lv_arc_create(active_cont);
    /* Diameter is set per state in ui_set_state(); this is just the seed. */
    lv_obj_set_size(active_arc, INDICATOR_SZ_ANALYZING, INDICATOR_SZ_ANALYZING);
    lv_obj_align(active_arc, LV_ALIGN_CENTER, 0, 0);
    lv_arc_set_rotation(active_arc, 270);          /* sweep starts at the top */
    lv_arc_set_bg_angles(active_arc, 0, 360);      /* full ring stays visible */
    lv_arc_set_angles(active_arc, 0, 20);
    lv_obj_set_style_arc_color(active_arc, UI_COLOR_CARD_BORDER, LV_PART_MAIN);
    lv_obj_set_style_arc_width(active_arc, 3, LV_PART_MAIN);
    lv_obj_set_style_arc_color(active_arc, UI_COLOR_INDICATOR, LV_PART_INDICATOR);
    lv_obj_set_style_arc_width(active_arc, 3, LV_PART_INDICATOR);
    lv_obj_remove_style(active_arc, NULL, LV_PART_KNOB);
    /* Without this the arc is draggable and touching it would set its value */
    lv_obj_clear_flag(active_arc, LV_OBJ_FLAG_CLICKABLE);

    active_label = lv_label_create(active_cont);
    lv_label_set_text(active_label, "Detecting.\nSign Now.");
    lv_obj_set_style_text_font(active_label, &lv_font_montserrat_14, LV_PART_MAIN);
    lv_obj_set_style_text_color(active_label, UI_COLOR_INDICATOR, LV_PART_MAIN);
    lv_obj_set_style_text_align(active_label, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_align(active_label, LV_ALIGN_CENTER, 0, 0);   /* centred in the arc */

    /* ----------------------------------------------------
     * 2. STATE: RESULT -- generated text only, no controls
     * ---------------------------------------------------- */
    result_cont = make_state_cont(center_main_cont);

    /*
     * The sentence is the one thing on this screen, and it is read across a
     * room, so it gets the biggest font the build already carries. 24 is free:
     * lv_conf.h enables it for the ERROR warning glyph, so this adds no flash.
     * 12/16/20 are compiled out -- enabling one costs a font table, so check
     * lv_conf.h before reaching for another size.
     *
     * Height is LV_SIZE_CONTENT rather than a fixed 176 so the label box hugs
     * its text. Centring a fixed-height box would leave the text sitting at the
     * top of it, which is exactly what this screen used to do.
     *
     * LONG_WRAP, not LONG_DOT: dots need a fixed height to know when to bite,
     * and a wrapped sentence reads better than a truncated one. The width stays
     * 200 inside a 216 container, so the worst case the protocol allows -- a
     * 120-char `cap_text` line -- wraps to about eight lines and still clears
     * the container's 260.
     */
    result_label = lv_label_create(result_cont);
    lv_label_set_long_mode(result_label, LV_LABEL_LONG_WRAP);
    lv_obj_set_size(result_label, 200, LV_SIZE_CONTENT);
    lv_label_set_text(result_label, "");
    lv_obj_set_style_text_font(result_label, &lv_font_montserrat_24, LV_PART_MAIN);
    lv_obj_set_style_text_color(result_label, UI_COLOR_TEXT_RESULT, LV_PART_MAIN);
    lv_obj_set_style_text_align(result_label, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    lv_obj_align(result_label, LV_ALIGN_CENTER, 0, 0);

    /* ----------------------------------------------------
     * 3. STATE: ERROR -- warning icon + message, no controls
     * ---------------------------------------------------- */
    error_cont = make_state_cont(center_main_cont);

    lv_obj_t *error_icon = lv_label_create(error_cont);
    lv_label_set_text(error_icon, LV_SYMBOL_WARNING);
    lv_obj_set_style_text_font(error_icon, &lv_font_montserrat_24, LV_PART_MAIN);
    lv_obj_set_style_text_color(error_icon, UI_COLOR_ERROR, LV_PART_MAIN);
    lv_obj_align(error_icon, LV_ALIGN_CENTER, 0, -50);

    error_label = lv_label_create(error_cont);
    lv_label_set_text(error_label, UI_ERROR_DEFAULT_TEXT);
    lv_obj_set_style_text_font(error_label, &lv_font_montserrat_18, LV_PART_MAIN);
    lv_obj_set_style_text_color(error_label, UI_COLOR_TEXT_ERROR, LV_PART_MAIN);
    lv_obj_align(error_label, LV_ALIGN_CENTER, 0, -8);

    /* Power-on sequence: BOOT (auto-advances) -> INTRO (waits for Start/Stop)
     * -> the capture loop. Stop returns to INTRO; BOOT is not revisited until
     * a reboot. */
    ui_set_state(UI_STATE_BOOT);

    return center_main_cont;
}
