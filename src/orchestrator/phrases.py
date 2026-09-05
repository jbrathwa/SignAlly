"""The phrase table: gloss in, English text and a wav path out.

Keyed by the gloss the model emits, and the key set must equal
the classifier's label map exactly. A gloss with no row is a recognised
sign the device cannot say; core.translate turns that into `unclear` rather
than putting a bare English token on screen as though it were a phrase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class PhraseTableError(Exception):
    """The table is missing or unusable. The service refuses to start."""


@dataclass(frozen=True)
class Phrase:
    gloss: str
    text: str
    audio: str


class PhraseTable:
    def __init__(self, rows: dict[str, Phrase]):
        self._rows = dict(rows)

    @classmethod
    def load(cls, path: Path, lang: str = "en") -> PhraseTable:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PhraseTableError(f"phrase table not found: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise PhraseTableError(f"phrase table unreadable: {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise PhraseTableError(f"phrase table must be a JSON object: {path}")

        rows: dict[str, Phrase] = {}
        for gloss, row in raw.items():
            try:
                text = row["text"][lang]
                audio = row["audio"][lang]
            except (TypeError, KeyError) as exc:
                raise PhraseTableError(
                    f"phrase table row {gloss!r} has no {lang!r} text/audio"
                ) from exc
            rows[gloss] = Phrase(gloss=gloss, text=text, audio=audio)
        return cls(rows)

    def get(self, gloss: str) -> Phrase | None:
        return self._rows.get(gloss)

    def glosses(self) -> frozenset[str]:
        return frozenset(self._rows)

    def __len__(self) -> int:
        return len(self._rows)
