"""The orchestrator's decisions, as pure functions.

No sockets, no clock, no counter. Every decision is a function of one event and
five fields of DeviceState, which is what lets the recorded fixtures replay
through here in a unit test with nothing running.

`seq` is stamped by the hub at emission. Audio is returned as an *intent* — a
gloss id — and the shell resolves it to a path and a subprocess. If this module
ever imports `socket` or `subprocess`, the boundary has been drawn wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .phrases import PhraseTable

SCREEN_IDLE = "idle"
SCREEN_LISTENING = "listening"
SCREEN_ANALYZING = "analyzing"


@dataclass(frozen=True)
class DeviceState:
    screen: str = SCREEN_IDLE
    capture_active: bool = False
    muted: bool = False
    tracking_status: str = "ok"
    offline: bool = False


@dataclass(frozen=True)
class Outcome:
    state: DeviceState
    messages: tuple[dict, ...] = ()
    audio: tuple[str, ...] = ()
    logs: tuple[tuple[str, str], ...] = ()


def state_message(state: DeviceState) -> dict:
    return {"t": "state", "s": state.screen}


def _to_screen(state: DeviceState, screen: str) -> Outcome:
    """State messages are emitted on change only — `recording` is per-frame."""
    if state.screen == screen:
        return Outcome(state)
    new = replace(state, screen=screen)
    return Outcome(new, messages=(state_message(new),))


def translate(event: dict, state: DeviceState, phrases: PhraseTable) -> Outcome:
    kind = event.get("e")

    if not state.capture_active:
        return Outcome(state, logs=(("debug", f"ignored {kind!r} while not capturing"),))

    if kind in ("armed", "recording"):
        return _to_screen(state, SCREEN_LISTENING)
    if kind == "classifying":
        return _to_screen(state, SCREEN_ANALYZING)

    return Outcome(state, logs=(("info", f"unhandled recognition event {kind!r}"),))
