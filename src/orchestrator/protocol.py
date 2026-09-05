"""Display protocol v1 framing. One JSON object per line, 256 bytes max.

The limit is not advisory: the STM32's rx buffer is sized to it, and a line
over it is silently truncated somewhere down the UART where nothing can report
the failure. Fail loudly here instead.
"""

from __future__ import annotations

import json

MAX_LINE = 256
MAX_TEXT = 120


class ProtocolError(Exception):
    """A message cannot be framed."""


def cap_text(text: str) -> str:
    """Truncate to the protocol's text limit. Callers log when this bites."""
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT]


def encode(msg: dict) -> bytes:
    line = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(line) + 1 > MAX_LINE:
        raise ProtocolError(f"line is {len(line) + 1} bytes, limit is {MAX_LINE}: {msg!r}")
    return line + b"\n"
