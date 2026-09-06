/**
 * @file config.h
 * Link settings for the panel relay.
 *
 * Deliberately tiny. The relay has no state machine, no vocabulary and no
 * opinions about the protocol -- the only thing it needs to know is how to
 * reach the panel. Everything else lives on the Linux side.
 *
 * The panel's matching values are in panel/protocol/config.h. The two must
 * agree on baud and framing; nothing checks that for you.
 */

#pragma once

/* ------------------------------------------------------------------------
 * UART to the CrowPanel display
 * ------------------------------------------------------------------------ */

/* Serial1 on an UNO-form-factor board is already routed to D0/D1, so begin()
 * needs no pin arguments. */
#define MCU_UART        Serial1
#define MCU_UART_BAUD   115200

/* Physical pins carrying that port -- documentation on this variant, since
 * Serial1 is hardwired to them.
 *
 *   D1 (TX) -> CrowPanel GPIO16 (RX)
 *   D0 (RX) <- CrowPanel GPIO17 (TX)
 *   GND     -- GND                     <- not optional
 */
#define MCU_RX_PIN      0
#define MCU_TX_PIN      1

/* ------------------------------------------------------------------------
 * Bridge method names
 * ------------------------------------------------------------------------
 *
 * These four strings are the whole contract with the Linux side. They must
 * match applab/signally-console/python/main.py exactly -- a typo here produces
 * a relay that runs, logs nothing unusual, and silently never forwards.
 */
#define RPC_DISPLAY_LINE  "display_line"   /* Python -> here -> panel */
#define RPC_PANEL_UPLINK  "panel_uplink"   /* panel  -> here -> Python */
#define RPC_PANEL_HELLO   "panel_hello"    /* Python probes for a live relay */

#define RELAY_FW_VERSION  "relay-0.1.0"

/* ------------------------------------------------------------------------
 * Framing
 * ------------------------------------------------------------------------ */

/*
 * Must not be smaller than the orchestrator's MAX_LINE (256, enforced in
 * orchestrator/src/orchestrator/protocol.py) or the relay becomes the thing
 * that truncates a line the orchestrator carefully kept short enough.
 */
#define UART_LINE_MAX  256
