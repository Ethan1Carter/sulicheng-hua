"""Edge-TTS speech service for TheoremExplainAgent.

Mirrors the interface of :class:`src.utils.kokoro_voiceover.KokoroService` so it
can be dropped into ``manim_voiceover``'s ``VoiceoverScene.set_speech_service``.
Kokoro v0.19 only ships English voices, so Chinese narration is synthesised with
Microsoft Edge's free neural voices (no API key, requires network access).
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from manim_voiceover.services.base import SpeechService

import edge_tts


# Sensible Simplified-Chinese defaults; override via env vars.
DEFAULT_VOICE = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
DEFAULT_SPEED = float(os.getenv("EDGE_TTS_SPEED", "1.0"))


def _speed_to_rate(speed: float) -> str:
    """Convert a 1.0-relative speed to Edge-TTS' ``+NN%`` / ``-NN%`` rate string."""
    percent = int(round((speed - 1.0) * 100))
    return f"{percent:+d}%"


class EdgeTTSService(SpeechService):
    """Speech service backed by Microsoft Edge neural TTS (via ``edge-tts``)."""

    def __init__(self,
                 voice: str = DEFAULT_VOICE,
                 speed: float = DEFAULT_SPEED,
                 **kwargs):
        self.voice = voice
        self.speed = speed
        super().__init__(**kwargs)

    def get_data_hash(self, input_data: dict) -> str:
        """Stable SHA-256 hash of the input data used as the cache key."""
        data_str = json.dumps(input_data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(data_str.encode("utf-8")).hexdigest()

    def _synthesize(self, text: str, output_mp3: str) -> None:
        """Synthesize ``text`` into an mp3 file at ``output_mp3``."""
        async def _run() -> None:
            communicate = edge_tts.Communicate(
                text, voice=self.voice, rate=_speed_to_rate(self.speed)
            )
            await communicate.save(output_mp3)

        try:
            asyncio.run(_run())
        except RuntimeError:
            # An event loop is already running (e.g. inside async pipeline):
            # fall back to a dedicated loop in this thread.
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_run())
            finally:
                loop.close()

    def generate_from_text(self, text: str, cache_dir: str = None, path: str = None) -> dict:
        if cache_dir is None:
            cache_dir = self.cache_dir

        input_data = {
            "input_text": text,
            "service": "edge_tts",
            "voice": self.voice,
        }
        cached_result = self.get_cached_result(input_data, cache_dir)
        if cached_result is not None:
            return cached_result

        if path is None:
            audio_path = self.get_data_hash(input_data) + ".mp3"
        else:
            audio_path = path

        # Edge-TTS writes mp3 directly, so no intermediate wav conversion is needed.
        mp3_audio_path = str(Path(cache_dir) / audio_path)
        self._synthesize(text, mp3_audio_path)

        return {
            "input_text": text,
            "input_data": input_data,
            "original_audio": audio_path,
        }
