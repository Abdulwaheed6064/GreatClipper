"""CPU transcription with faster-whisper (word timestamps + VAD filtering).

The model is loaded lazily, once per worker process. Transcripts are stored as
JSON next to the source and reused for identical media (same file hash).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    language: str
    language_probability: float
    duration: float
    segments: list[Segment]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Transcript":
        d = json.loads(text)
        segs = [Segment(s["start"], s["end"], s["text"], [Word(**w) for w in s["words"]]) for s in d["segments"]]
        return cls(d["language"], d["language_probability"], d["duration"], segs)

    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def text_between(self, start: float, end: float) -> str:
        return " ".join(w.text.strip() for w in self.words() if w.start >= start - 0.05 and w.end <= end + 0.05)

    def speech_seconds(self) -> float:
        return sum(max(0.0, s.end - s.start) for s in self.segments)


class Transcriber(Protocol):
    model_name: str

    def transcribe(self, path: Path) -> Transcript: ...


class WhisperTranscriber:
    def __init__(self, model_name: str = "small", compute_type: str = "int8"):
        self.model_name = model_name
        self.compute_type = compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute_type)
        return self._model

    def transcribe(self, path: Path) -> Transcript:
        model = self._load()
        segments, info = model.transcribe(
            str(path),
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,  # reduces runaway hallucinated text over music
        )
        out = []
        for s in segments:
            words = [Word(float(w.start), float(w.end), w.word, float(w.probability)) for w in (s.words or [])]
            out.append(Segment(float(s.start), float(s.end), s.text.strip(), words))
        return Transcript(info.language, float(info.language_probability), float(info.duration), out)


def transcript_quality_problems(t: Transcript, min_language_probability: float) -> list[str]:
    """Poor audio / unsupported language → review, never auto-approve invented words."""
    p = []
    if t.language_probability < min_language_probability:
        p.append(f"low language confidence ({t.language} p={t.language_probability:.2f})")
    words = t.words()
    if not words:
        p.append("no speech detected")
    else:
        low = sum(1 for w in words if w.prob < 0.4) / len(words)
        if low > 0.35:
            p.append(f"{low:.0%} of words are low-confidence (poor audio?)")
    return p
