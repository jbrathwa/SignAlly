/**
 * @file ui_theme.h
 * Centralized Color Theme System for Elecrow CrowPanel 2.8" HMI (LVGL v8)
 * Edit this header to reskin the entire UI layout.
 */

#ifndef UI_THEME_H
#define UI_THEME_H

#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Screen & Panel Background Colors */
#define UI_COLOR_BG                lv_color_hex(0x121218)
#define UI_COLOR_STATUS_BAR_BG     lv_color_hex(0x1A1A26)
/* #define UI_COLOR_CARD_BG        lv_color_hex(0x1E1E2C) */ /* unused: no card surfaces remain */
#define UI_COLOR_CARD_BORDER       lv_color_hex(0x2D2D42)  /* arc track, status bar rule */

/* Icon Tint Colors */
#define UI_COLOR_ICON_IDLE         lv_color_hex(0x7A7A90)
#define UI_COLOR_ICON_ACTIVE       lv_color_hex(0x00E5FF)  /* Mic & speaker active */
#define UI_COLOR_ICON_ACTIVE_CAM   lv_color_hex(0x34C759)  /* Camera active (green) */

/* Battery Level Band Colors */
#define UI_COLOR_BATTERY_CRITICAL  lv_color_hex(0xFF3B30)  /*  0% - 15% */
#define UI_COLOR_BATTERY_LOW       lv_color_hex(0xFF9500)  /* 15% - 35% */
#define UI_COLOR_BATTERY_MEDIUM    lv_color_hex(0xFFCC00)  /* 35% - 65% */
#define UI_COLOR_BATTERY_HIGH      lv_color_hex(0x34C759)  /* 65% - 100% */

/* Error State Colors */
#define UI_COLOR_ERROR             lv_color_hex(0xFF453A)

/* Center Section Text & Indicator Colors */
/* LISTENING and ANALYZING deliberately share one colour: they are told apart
 * by indicator diameter and label text, not by hue. */
#define UI_COLOR_INDICATOR         lv_color_hex(0x00E5FF)
#define UI_COLOR_TEXT_RESULT       lv_color_hex(0xF0F0F5)
#define UI_COLOR_TEXT_ERROR        lv_color_hex(0xFF453A)

/* Button Colors -- COMMENTED OUT, not deleted.
 *
 * Every on-screen control went away when Start/Stop and mic mute moved to
 * physical buttons, so nothing references these. Kept here so a future button
 * can match the original palette exactly rather than being re-invented.
 * Uncomment to use. */
/* #define UI_COLOR_BTN_PRIMARY    lv_color_hex(0x3A3A4C) */ /* Emphasised action */
/* #define UI_COLOR_BTN_SECONDARY  lv_color_hex(0x24242F) */ /* Secondary action */
/* #define UI_COLOR_BTN_TEXT       lv_color_hex(0xF0F0F5) */ /* Label on a button */

#ifdef __cplusplus
}
#endif

#endif /* UI_THEME_H */
