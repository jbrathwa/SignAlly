/**
 * @file demo_manager.h
 * LittleFS JSON Demo Manager Header for Elecrow CrowPanel 2.8" HMI
 *
 * BENCH DEBUG PATH ONLY. Nothing here runs unless you ask for it by typing
 * "reload" on the USB console.
 *
 * The UI is owned by the MCU over the UART link -- `state` and `result` drive
 * the centre screen, `status` drives the battery and mute icons (see
 * docs/WIRE_PROTOCOL.md). demo_state.json used to seed those at boot, which
 * meant a panel with a dead link sat there showing convincing-looking demo
 * values and no way to tell. It no longer applies anything on its own.
 */

#ifndef DEMO_MANAGER_H
#define DEMO_MANAGER_H

#include <Arduino.h>

#ifdef __cplusplus
extern "C" {
#endif
#include "ui.h"
#include "buttons.h"
#ifdef __cplusplus
}
#endif

/**
 * @brief Mount LittleFS so a later "reload" has something to read.
 *
 * Mounts only -- it deliberately does NOT apply the file. See the file header.
 *
 * @return true if the filesystem mounted, false on error.
 */
bool demo_manager_init(void);

/**
 * @brief Read /demo_state.json and force its values onto the UI, right now.
 *
 * A manual override for bench work with no MCU attached. It will happily
 * overwrite whatever the MCU last sent; the MCU is not told, and will not
 * resend, so the two are out of step until the next message arrives.
 *
 * @return true if successful, false on error.
 */
bool demo_manager_reload(void);

/**
 * @brief Check serial input buffer for "reload" command.
 */
void demo_manager_check_serial(void);

#endif /* DEMO_MANAGER_H */
