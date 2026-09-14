"""Voice-message transcription for @CariOra_bot.

Uses faster-whisper — runs locally on CPU, no per-call API cost (matches
the project's cheapest-tier discipline: zero marginal cost beats even
Claude Haiku for this). Model weights download once from Hugging Face on
first use (~140MB for "base") and are cached under ~/.cache/huggingface.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
_WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "ru")

_model = None  # lazy-loaded — import + model download is slow, skip if voice unused


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        logger.info("loading faster-whisper model %r", _WHISPER_MODEL_SIZE)
        _model = WhisperModel(_WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def transcribe(audio_bytes: bytes) -> Optional[str]:
    """Transcribes an OGG/Opus voice note (Telegram's voice format) to text.
    Returns None on failure — caller should tell the user to type instead
    of crashing the flow."""
    try:
        model = _get_model()
        segments, _info = model.transcribe(
            io.BytesIO(audio_bytes),
            language=_WHISPER_LANGUAGE,
            vad_filter=True,  # trims silence, keeps short voice notes fast
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text or None
    except Exception:
        logger.exception("voice transcription failed")
        return None
