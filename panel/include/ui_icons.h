/**
 * @file ui_icons.h
 * Aggregated Icon Image Descriptors & Symbol Definitions for LVGL HMI
 */

#ifndef UI_ICONS_H
#define UI_ICONS_H

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Icon image assets (defined in src/ui_icons.c).
 *
 * All four are LV_IMG_CF_ALPHA_8BIT, 20x20. Alpha-only images carry no colour
 * of their own: LVGL blends them as a solid fill of the object's `img_recolor`
 * style, using the asset bytes as an antialiased coverage mask. One asset
 * therefore serves both the idle and the active tint -- recolouring is the
 * whole state model, no second bitmap is needed.
 *
 * When attaching one to an lv_img you MUST also set `img_recolor_opa` to
 * LV_OPA_COVER. LVGL only applies `img_recolor` when the recolor opacity is
 * non-zero, so leaving it at the default renders the icon black on black.
 *
 * Source artwork lives in assets/icons/; regenerate both the PNGs and
 * src/ui_icons.c with `python tools/gen_icons.py`.
 */
extern const lv_img_dsc_t ui_img_camera;
extern const lv_img_dsc_t ui_img_mic;

/*
 * The speaker has two glyphs because recolouring alone cannot separate its
 * states: the muted variant carries a cross, and tinting a crossed-out
 * speaker "active" would read as "muted, but cyan". The plain variant carries
 * a sound wave and is used only while TTS is playing.
 */
extern const lv_img_dsc_t ui_img_speaker;        /* plain + wave  -> TTS playing */
extern const lv_img_dsc_t ui_img_speaker_muted;  /* cone + cross  -> muted      */

#ifdef __cplusplus
}
#endif

#endif /* UI_ICONS_H */
