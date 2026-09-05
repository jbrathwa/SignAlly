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
from .protocol import cap_text

SCREEN_IDLE = "idle"
SCREEN_LISTENING = "listening"
SCREEN_ANALYZING = "analyzing"

TRACKING_TEXT = {"clipped": "Move back", "absent": "Nobody in frame"}


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


def _recognised(event: dict, state: DeviceState, phrases: PhraseTable) -> Outcome:
    gloss = event.get("gloss")
    conf = event.get("conf", 0.0)
    logs = [("info", f"recognised {gloss!r} conf={conf} top3={event.get('top3')}")]
    if event.get("take_usable") is False:
        logs.append(("warning", f"take_usable=False for {gloss!r} — camera saw it poorly"))

    phrase = phrases.get(gloss)
    if phrase is None:
        # A recognised sign the device cannot say. `unclear` is the honest answer;
        # a bare English gloss on screen would read as a phrase it never said.
        logs.append(("warning", f"no phrase row for gloss {gloss!r}; emitting unclear"))
        return Outcome(state, messages=({"t": "unclear", "conf": conf},), logs=tuple(logs))

    message = {"t": "result", "id": gloss, "text": cap_text(phrase.text), "conf": conf}
    return Outcome(state, messages=(message,), audio=(gloss,), logs=tuple(logs))


def _unclear(event: dict, state: DeviceState) -> Outcome:
    logs = [("info", f"unclear conf={event.get('conf', 0.0)} "
                     f"reason={event.get('reason')} n_frames={event.get('n_frames')}")]
    return Outcome(state, messages=({"t": "unclear", "conf": event.get("conf", 0.0)},),
                   logs=tuple(logs))


def _tracking(event: dict, state: DeviceState) -> Outcome:
    status = event.get("status")
    if status == state.tracking_status:
        return Outcome(state)

    was_error = state.tracking_status in TRACKING_TEXT
    new = replace(state, tracking_status=status)

    if status in TRACKING_TEXT:
        return Outcome(new, messages=({"t": "error", "text": TRACKING_TEXT[status]},),
                       logs=(("info", f"tracking {status}"),))

    # Recovery the protocol spec does not describe: `error` is a payload message
    # and there is no "error over", so re-sending state is the only v1 way to get
    # the panel off the error screen.
    if was_error:
        return Outcome(new, messages=(state_message(new),),
                       logs=(("info", f"tracking {status}; clearing error screen"),))

    return Outcome(new, logs=(("info", f"tracking {status}"),))


def translate(event: dict, state: DeviceState, phrases: PhraseTable) -> Outcome:
    kind = event.get("e")

    if kind == "fault":
        text = cap_text(event.get("msg") or "Recognition fault")
        return Outcome(
            state,
            messages=({"t": "error", "text": text},),
            logs=(("error", f"fault {event.get('code')!r}: {event.get('msg')!r}"),),
        )

    if not state.capture_active:
        return Outcome(state, logs=(("debug", f"ignored {kind!r} while not capturing"),))

    if kind in ("armed", "recording"):
        return _to_screen(state, SCREEN_LISTENING)
    if kind == "classifying":
        return _to_screen(state, SCREEN_ANALYZING)
    if kind == "recognised":
        return _recognised(event, state, phrases)
    if kind == "unclear":
        return _unclear(event, state)

    if kind == "tracking":
        return _tracking(event, state)

    return Outcome(state, logs=(("info", f"unhandled recognition event {kind!r}"),))


def mark_offline(state: DeviceState) -> Outcome:
    """Emitted once per outage, not once per check."""
    if state.offline:
        return Outcome(state)
    return Outcome(
        replace(state, offline=True),
        messages=({"t": "error", "text": "Recognition offline"},),
        logs=(("error", "recognition stream went quiet"),),
    )


def mark_online(state: DeviceState) -> Outcome:
    if not state.offline:
        return Outcome(state)
    new = replace(state, offline=False)
    return Outcome(new, messages=(state_message(new),),
                   logs=(("info", "recognition stream back"),))
