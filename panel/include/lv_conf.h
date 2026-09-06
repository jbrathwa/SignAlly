/**
 * @file lv_conf.h
 * Configuration file for LVGL v8.3.3 optimized for Elecrow CrowPanel 2.8" ESP32 HMI
 */

#ifndef LV_CONF_H
#define LV_CONF_H

#include <stdint.h>

/*====================
   COLOR SETTINGS
 *====================*/

/* Color depth: 1 (1 byte per pixel), 8 (RGB332), 16 (RGB565), 32 (ARGB8888) */
#define LV_COLOR_DEPTH 16

/* Swap the 2 bytes of RGB565 color. Needed for ILI9341 SPI display */
#define LV_COLOR_16_SWAP 1

/* Enable features for 16-bit color depth */
#define LV_COLOR_MIX_ROUND_OFS (LV_COLOR_DEPTH == 32 ? 0 : 128)
#define LV_COLOR_CHROMA_KEY lv_color_hex(0x00FF00)

/*=========================
   MEMORY SETTINGS
 *=========================*/

/* 1: use custom malloc/free, 0: use LVGL's internal memory pool */
#define LV_MEM_CUSTOM 0
#if LV_MEM_CUSTOM == 0
    /* Size of the memory available for `lv_mem_alloc()` in bytes (32 kB) */
    #define LV_MEM_SIZE (32U * 1024U)

    /* Set an address for the memory pool instead of allocating it as a static array */
    #define LV_MEM_ADR 0

    /* Align memory allocation to bytes */
    #define LV_MEM_POOL_INCLUDE <stdint.h>
    #define LV_MEM_POOL_ALLOC   malloc
    #define LV_MEM_POOL_FREE    free
#endif

/* Number of the slots in `lv_draw_mask` optimization pool */
#define LV_DRAW_COMPLEX 1
#if LV_DRAW_COMPLEX != 0
    #define LV_MAX_RES 320
    #define LV_DRAW_PARAM_MAX_CACHE_NUM 16
#endif

/*====================
   HAL SETTINGS
 *====================*/

/* Default display refresh period in milliseconds */
#define LV_DISP_DEF_REFR_PERIOD 16

/* Input device read period in milliseconds */
#define LV_INDEV_DEF_READ_PERIOD 16

/* Use a custom tick source */
#define LV_TICK_CUSTOM 0

/* Default Dot Per Inch. Used to scale sizes */
#define LV_DPI_DEF 130

/*=======================
 * FEATURE CONFIGURATION
 *=======================*/

/* Drawing features */
#define LV_USE_LOG 0

/* Asserts */
#define LV_USE_ASSERT_NULL          1
#define LV_USE_ASSERT_MALLOC        1
#define LV_USE_ASSERT_STYLE         0
#define LV_USE_ASSERT_MEM_INTEGRITY 0
#define LV_USE_ASSERT_OBJ           0

/* Others */
#define LV_USE_PERF_MONITOR         0
#define LV_USE_MEM_MONITOR          0
#define LV_USE_REFR_DEBUG           0

/*==================
 * WIDGET USAGE
 *==================*/

#define LV_USE_ARC          1
#define LV_USE_BAR          1
#define LV_USE_BTN          1
#define LV_USE_BTNMATRIX    1
#define LV_USE_CANVAS       1
#define LV_USE_CHECKBOX     1
#define LV_USE_DROPDOWN     1
#define LV_USE_IMG          1
#define LV_USE_LABEL        1
#define LV_USE_LINE         1
#define LV_USE_ROLLER       1
#define LV_USE_SLIDER       1
#define LV_USE_SWITCH       1
#define LV_USE_TEXTAREA     1
#define LV_USE_TABLE        1

/*==================
 * EXTRA COMPONENTS
 *==================*/

#define LV_USE_ANIMIMG      1
#define LV_USE_CALENDAR     1
#define LV_USE_CHART        1
#define LV_USE_COLORWHEEL   1
#define LV_USE_IMGBTN       1
#define LV_USE_KEYBOARD     1
#define LV_USE_LED          1
#define LV_USE_LIST         1
#define LV_USE_MENU         1
#define LV_USE_METER        1
#define LV_USE_MSGBOX       1
#define LV_USE_SPAN         1
#define LV_USE_SPINBOX      1
#define LV_USE_SPINNER      1
#define LV_USE_TABVIEW      1
#define LV_USE_TILEVIEW     1
#define LV_USE_WIN          1

/*==================
 * FONT USAGE
 *==================*/

/* Only the three sizes the UI actually draws with are built in. 12/16/20 went
 * unused when the on-screen buttons were removed -- each font costs flash
 * whether it is drawn or not. Flip one back to 1 if you need it; leaving it at
 * 0 and referencing the symbol is a link error, not a silent fallback. */
#define LV_FONT_MONTSERRAT_12 0   /* spare: smaller label option */
#define LV_FONT_MONTSERRAT_14 1   /* body text, state labels */
#define LV_FONT_MONTSERRAT_16 0   /* was the Idle screen's Start button */
#define LV_FONT_MONTSERRAT_18 1   /* error message */
#define LV_FONT_MONTSERRAT_20 0   /* spare */
#define LV_FONT_MONTSERRAT_24 1   /* error warning glyph */

#define LV_FONT_DEFAULT &lv_font_montserrat_14

/*===================
 * THEMES
 *===================*/

#define LV_USE_THEME_DEFAULT 1
#if LV_USE_THEME_DEFAULT
    #define LV_THEME_DEFAULT_DARK 1
    #define LV_THEME_DEFAULT_GROW 1
    #define LV_THEME_DEFAULT_TRANSITION_TIME 80
#endif

#define LV_USE_THEME_BASIC 1

#endif /* LV_CONF_H */
