/**
 * @file capture_stub.h
 * TEMPORARY placeholder for the real landmark-capture / backend-response
 * pipeline. Everything in this module is a fixed-delay stand-in for modules
 * that don't exist yet and is expected to be deleted once module 1 (capture)
 * and the backend API are ready.
 */

#ifndef CAPTURE_STUB_H
#define CAPTURE_STUB_H

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Poll the capture-stub state machine. Call once per loop() iteration,
 * after buttons_poll() so a Start/Stop press this tick is already reflected.
 */
void capture_stub_poll(void);

#ifdef __cplusplus
}
#endif

#endif /* CAPTURE_STUB_H */
