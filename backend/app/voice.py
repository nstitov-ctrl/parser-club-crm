"""Voice-message transcription for @CariOra_bot.

Uses faster-whisper — runs locally on CPU, no per-call API cost (matches
the project's cheapest-tier discipline: zero marginal cost beats even
Claude Haiku for this). Model weights (~140MB for "base") are cached under
~/.cache/huggingface; the Railway build pre-downloads them (see
railway.json) and bot.run() loads the model at startup (preload), so the
first voice note after a deploy doesn't wait for a download.

Failures are reported as VoiceError with a short code ("load:OSError",
"decode:InvalidDataError", ...) so the bot can show WHY it failed instead
of one generic message — on a hosted bot the logs aren't always at hand.
"""
from __future__ import annotations

import io
import logging
import os
import threading

logger = logging.getLogger(__name__)

_WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
_WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "ru")

_model = None  # lazy-loaded — import + model download is slow, skip if voice unused
_model_lock = threading.Lock()


class VoiceError(Exception):
    """Transcription machinery failed (not "no speech found" — that's an
    empty string). `code` is short and safe to show to the user."""

    def __init__(self, stage: str, cause: BaseException):
        self.code = f"{stage}:{type(cause).__name__}"
        super().__init__(f"{self.code}: {cause}")


def _get_model():
    global _model
    with _model_lock:                      # preload thread + first voice note must not load twice
        if _model is None:
            try:
                from faster_whisper import WhisperModel

                logger.info("loading faster-whisper model %r", _WHISPER_MODEL_SIZE)
                _model = WhisperModel(_WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
                logger.info("faster-whisper model loaded")
            except Exception as exc:
                logger.exception("faster-whisper model load failed")
                raise VoiceError("load", exc) from exc
        return _model


def preload() -> None:
    """Loads the model ahead of the first voice note. Never raises — a failed
    preload just means the first voice note retries the load (and reports it)."""
    try:
        _get_model()
    except VoiceError:
        pass


def _run(model, audio_bytes: bytes, vad: bool) -> str:
    segments, _info = model.transcribe(
        io.BytesIO(audio_bytes),
        language=_WHISPER_LANGUAGE,
        vad_filter=vad,  # trims silence, keeps short voice notes fast
    )
    return " ".join(segment.text.strip() for segment in segments).strip()


def transcribe(audio_bytes: bytes) -> str:
    """Transcribes an OGG/Opus voice note (Telegram's voice format) to text.
    Returns "" when no speech was found; raises VoiceError when the
    machinery itself failed."""
    model = _get_model()
    try:
        text = _run(model, audio_bytes, vad=True)
        if not text:
            # VAD can swallow very short / quiet notes — retry on the raw audio
            # before telling the user we heard nothing.
            text = _run(model, audio_bytes, vad=False)
        return text
    except Exception as exc:
        logger.exception("voice transcription failed")
        raise VoiceError("decode", exc) from exc
