/**
 * @file ui_status_bar.c
 * Status Bar (Top Right-Aligned) Implementation for Portrait 240x320 HMI
 */

#include "ui_status_bar.h"
#include <stdio.h>

/* Widget Pointers */
static lv_obj_t *status_bar_cont = NULL;
static lv_obj_t *cam_img = NULL;
static lv_obj_t *mic_img = NULL;
static lv_obj_t *speaker_img = NULL;

/* Composite Battery Widget */
static lv_obj_t *bat_cont = NULL;
static lv_obj_t *bat_bar = NULL;

void ui_cam_set_state(cam_state_t state)
{
    if (cam_img == NULL) return;
    lv_color_t color = (state == CAM_ACTIVE) ? UI_COLOR_ICON_ACTIVE_CAM : UI_COLOR_ICON_IDLE;
    lv_obj_set_style_img_recolor(cam_img, color, LV_PART_MAIN);
}

/*
 * Mic and speaker are presence-only indicators now: visible means muted,
 * absent means running normally. The status bar is a right-aligned flex row
 * and LVGL skips LV_OBJ_FLAG_HIDDEN children when laying one out, so hiding an
 * icon closes its gap and the battery keeps its position against the edge.
 */
/*
 * CURRENTLY INERT. The mic widget is commented out in ui_status_bar_create(),
 * so mic_img is NULL and this returns immediately on every call. Kept live
 * rather than commented out because buttons.c calls it -- the NULL guard is
 * what makes hiding the icon a one-block change instead of a cross-file one.
 * The mute flag itself is unaffected and still tracks the physical button.
 */
void ui_mic_set_state(mic_state_t state)
{
    if (mic_img == NULL) return;

    if (state == MIC_MUTED) {
        lv_obj_clear_flag(mic_img, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(mic_img, LV_OBJ_FLAG_HIDDEN);
    }
}

/*
 * Speaker icon state.
 *
 * Three independent inputs decide this icon, so one refresh function owns the
 * whole decision. With each setter poking the object directly, whichever was
 * called last would clobber the others.
 *
 *   spk_in_use      RESULT screen only -- the speaker is idle everywhere else
 *   spk_muted       physical mute button on the enclosure
 *   spk_tts_playing hook for a future TTS engine; nothing sets it today
 */
static bool spk_in_use = false;
static bool spk_muted = false;
static bool spk_tts_playing = false;

/*
 * TTS "playing" pulse. Opacity was chosen over glyph cycling: it is a single
 * style property animated by LVGL's own timer, so there is no per-frame shape
 * redraw and no second timer to keep in step with the state machine. ~0.9s
 * round trip -- half fading out, half fading back.
 */
#define SPK_PULSE_HALF_MS   450
#define SPK_PULSE_OPA_MIN   LV_OPA_40

static lv_anim_t spk_pulse_anim;
static bool spk_pulse_running = false;

static void spk_pulse_anim_cb(void *var, int32_t v)
{
    lv_obj_set_style_img_opa((lv_obj_t *)var, (lv_opa_t)v, LV_PART_MAIN);
}

static void speaker_pulse_stop(void)
{
    if (speaker_img == NULL) return;

    lv_anim_del(speaker_img, spk_pulse_anim_cb);
    lv_obj_set_style_img_opa(speaker_img, LV_OPA_COVER, LV_PART_MAIN);
    spk_pulse_running = false;
}

static void speaker_pulse_start(void)
{
    /* Guarded: refresh() runs on every state change, and restarting the
     * animation each time would reset its phase and make it stutter. */
    if (speaker_img == NULL || spk_pulse_running) return;

    lv_anim_init(&spk_pulse_anim);
    lv_anim_set_var(&spk_pulse_anim, speaker_img);
    lv_anim_set_values(&spk_pulse_anim, SPK_PULSE_OPA_MIN, LV_OPA_COVER);
    lv_anim_set_time(&spk_pulse_anim, SPK_PULSE_HALF_MS);
    lv_anim_set_playback_time(&spk_pulse_anim, SPK_PULSE_HALF_MS);
    lv_anim_set_repeat_count(&spk_pulse_anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_path_cb(&spk_pulse_anim, lv_anim_path_ease_in_out);
    lv_anim_set_exec_cb(&spk_pulse_anim, spk_pulse_anim_cb);
    lv_anim_start(&spk_pulse_anim);

    spk_pulse_running = true;
}

static void speaker_icon_refresh(void)
{
    if (speaker_img == NULL) return;

    const bool show = spk_in_use && (spk_muted || spk_tts_playing);

    if (!show) {
        speaker_pulse_stop();
        lv_obj_add_flag(speaker_img, LV_OBJ_FLAG_HIDDEN);
        return;
    }

    if (spk_muted) {
        /* Mute takes visual priority over playback: audio through a muted
         * speaker is inaudible, so a "playing" indicator would be a lie.
         * Static crossed glyph, no pulse -- unchanged existing behaviour. */
        speaker_pulse_stop();
        lv_img_set_src(speaker_img, &ui_img_speaker_muted);
        lv_obj_set_style_img_recolor(speaker_img, UI_COLOR_ICON_IDLE, LV_PART_MAIN);
    } else {
        /* TTS playing, not muted: plain wave glyph in the active tint. */
        lv_img_set_src(speaker_img, &ui_img_speaker);
        lv_obj_set_style_img_recolor(speaker_img, UI_COLOR_ICON_ACTIVE, LV_PART_MAIN);
        speaker_pulse_start();
    }

    lv_obj_clear_flag(speaker_img, LV_OBJ_FLAG_HIDDEN);
}

void ui_speaker_set_state(speaker_state_t state)
{
    spk_muted = (state == SPEAKER_MUTED);
    speaker_icon_refresh();
}

void ui_speaker_set_in_use(bool in_use)
{
    spk_in_use = in_use;
    speaker_icon_refresh();
}

void ui_speaker_set_tts_playing(bool playing)
{
    spk_tts_playing = playing;
    speaker_icon_refresh();
}

bool ui_speaker_tts_is_playing(void)
{
    return spk_tts_playing;
}

void ui_battery_set_value(uint8_t percent)
{
    if (bat_bar == NULL) return;

    if (percent > 100) percent = 100;

    /* 1. Update Fill Level */
    lv_bar_set_value(bat_bar, percent, LV_ANIM_ON);

    /* 2. Map 4-tier Color Band */
    lv_color_t band_color;
    if (percent <= 15) {
        band_color = UI_COLOR_BATTERY_CRITICAL;
    } else if (percent <= 35) {
        band_color = UI_COLOR_BATTERY_LOW;
    } else if (percent <= 65) {
        band_color = UI_COLOR_BATTERY_MEDIUM;
    } else {
        band_color = UI_COLOR_BATTERY_HIGH;
    }

    /* 3. Apply color band to indicator bar fill */
    lv_obj_set_style_bg_color(bat_bar, band_color, LV_PART_INDICATOR);
}

lv_obj_t* ui_status_bar_create(lv_obj_t *parent)
{
    /* Main Status Bar Container (240x36 Pinned to Top) */
    status_bar_cont = lv_obj_create(parent);
    lv_obj_set_size(status_bar_cont, 240, 36);
    lv_obj_align(status_bar_cont, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_style_bg_color(status_bar_cont, UI_COLOR_STATUS_BAR_BG, LV_PART_MAIN);
    lv_obj_set_style_border_side(status_bar_cont, LV_BORDER_SIDE_BOTTOM, LV_PART_MAIN);
    lv_obj_set_style_border_color(status_bar_cont, UI_COLOR_CARD_BORDER, LV_PART_MAIN);
    lv_obj_set_style_border_width(status_bar_cont, 1, LV_PART_MAIN);
    lv_obj_set_style_radius(status_bar_cont, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(status_bar_cont, 4, LV_PART_MAIN);
    lv_obj_clear_flag(status_bar_cont, LV_OBJ_FLAG_SCROLLABLE);

    /* Horizontal Flex Layout for Icons (Right Aligned) */
    lv_obj_set_flex_flow(status_bar_cont, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(status_bar_cont, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    /* Uniform gap between Mic | Speaker | Battery, replacing per-child padding */
    lv_obj_set_style_pad_column(status_bar_cont, 10, LV_PART_MAIN);

    /* --------------------------------------------------
     * 1. CAMERA ICON WIDGET (TOP-LEFT)
     *
     * Lives in the status bar so it shares the icon row's vertical centring,
     * but is exempted from the right-aligned flex layout via IGNORE_LAYOUT so
     * it can be pinned to the left edge instead of joining the icon cluster.
     * -------------------------------------------------- */
    cam_img = lv_img_create(status_bar_cont);
    lv_img_set_src(cam_img, &ui_img_camera);
    lv_obj_add_flag(cam_img, LV_OBJ_FLAG_IGNORE_LAYOUT);
    lv_obj_align(cam_img, LV_ALIGN_LEFT_MID, 2, 0);
    /* Required for img_recolor to take effect at all -- see ui_icons.h */
    lv_obj_set_style_img_recolor_opa(cam_img, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_img_recolor(cam_img, UI_COLOR_ICON_IDLE, LV_PART_MAIN);

    /* --------------------------------------------------
     * 2. MIC ICON WIDGET -- COMMENTED OUT (hidden for now)
     *
     * The widget is simply never built, so `mic_img` stays NULL. Every entry
     * point already guards on that, which means nothing else has to change:
     * the physical mic mute button still toggles its flag and
     * buttons_set_mic_muted() still works -- they just paint nothing.
     *
     * Uncomment this block to bring the icon back. No other file needs
     * touching, and the slashed-mic artwork is still generated and linked.
     * -------------------------------------------------- */
    /*
    mic_img = lv_img_create(status_bar_cont);
    lv_img_set_src(mic_img, &ui_img_mic);
    lv_obj_set_style_img_recolor_opa(mic_img, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_img_recolor(mic_img, UI_COLOR_ICON_IDLE, LV_PART_MAIN);
    */

    /* --------------------------------------------------
     * 3. SPEAKER ICON WIDGET
     * -------------------------------------------------- */
    speaker_img = lv_img_create(status_bar_cont);
    /* Seeded muted; speaker_icon_refresh() swaps in the plain wave glyph if
     * TTS is ever playing while unmuted. */
    lv_img_set_src(speaker_img, &ui_img_speaker_muted);
    /* Required for img_recolor to take effect at all -- see ui_icons.h */
    lv_obj_set_style_img_recolor_opa(speaker_img, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_img_recolor(speaker_img, UI_COLOR_ICON_IDLE, LV_PART_MAIN);

    /* --------------------------------------------------
     * 4. COMPOSITE BATTERY WIDGET (Icon only, no percentage number)
     * -------------------------------------------------- */
    bat_cont = lv_obj_create(status_bar_cont);
    lv_obj_set_size(bat_cont, 34, 24);
    lv_obj_set_style_bg_opa(bat_cont, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_border_width(bat_cont, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(bat_cont, 0, LV_PART_MAIN);
    lv_obj_clear_flag(bat_cont, LV_OBJ_FLAG_SCROLLABLE);

    /* Battery Outer Shell */
    lv_obj_t *shell = lv_obj_create(bat_cont);
    lv_obj_set_size(shell, 30, 14);
    lv_obj_align(shell, LV_ALIGN_LEFT_MID, 0, 0);
    lv_obj_set_style_bg_opa(shell, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_border_color(shell, UI_COLOR_ICON_IDLE, LV_PART_MAIN);
    lv_obj_set_style_border_width(shell, 1, LV_PART_MAIN);
    lv_obj_set_style_radius(shell, 3, LV_PART_MAIN);
    lv_obj_set_style_pad_all(shell, 1, LV_PART_MAIN);
    lv_obj_clear_flag(shell, LV_OBJ_FLAG_SCROLLABLE);

    /* Battery Fill Bar */
    bat_bar = lv_bar_create(shell);
    lv_obj_set_size(bat_bar, 26, 10);
    lv_obj_center(bat_bar);
    lv_bar_set_range(bat_bar, 0, 100);
    lv_obj_set_style_bg_color(bat_bar, UI_COLOR_BATTERY_HIGH, LV_PART_INDICATOR);
    lv_obj_set_style_radius(bat_bar, 2, LV_PART_INDICATOR);

    /* Battery Nub Cap */
    lv_obj_t *nub = lv_obj_create(bat_cont);
    lv_obj_set_size(nub, 3, 6);
    lv_obj_align(nub, LV_ALIGN_LEFT_MID, 30, 0);
    lv_obj_set_style_bg_color(nub, UI_COLOR_ICON_IDLE, LV_PART_MAIN);
    lv_obj_set_style_border_width(nub, 0, LV_PART_MAIN);

    /* Initialize default states -- speaker starts hidden, battery always on */
    ui_cam_set_state(CAM_OFF);
    /* ui_mic_set_state(MIC_UNMUTED); */  /* inert: mic widget is commented out above */
    ui_speaker_set_state(SPEAKER_UNMUTED);
    ui_battery_set_value(100);

    return status_bar_cont;
}
