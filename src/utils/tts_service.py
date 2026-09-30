"""TTS engine selection for TheoremExplainAgent.

The generated Manim code calls :func:`get_speech_service` to obtain a voiceover
backend. The choice is driven by the ``TEA_NARRATION_LANG`` environment variable
so the same generated code works for both languages:

* ``TEA_NARRATION_LANG=zh`` (or any ``zh*``) -> :class:`EdgeTTSService` (Chinese)
* anything else / unset -> :class:`KokoroService` (English, upstream default)

This keeps English behaviour identical to upstream when the variable is unset.
"""

import os


def _wants_chinese() -> bool:
    return os.getenv("TEA_NARRATION_LANG", "en").strip().lower().startswith("zh")


def _configure_cjk_tex() -> None:
    """Switch Manim's global Tex template to a CJK-capable one in Chinese mode.

    Upstream keeps every on-screen ``Tex``/``MathTex`` in English because the
    default ``latex`` engine cannot typeset Chinese. In Chinese mode we instead
    point Manim at :data:`TexTemplateLibrary.ctex` (xelatex + ctex), which renders
    Chinese titles/labels fine. This runs on import, so every generated scene --
    which always ``import``s this module -- picks it up before ``construct`` builds
    any Tex object. Degrades silently to the English template if xelatex/ctex are
    missing, so a bare environment still produces (English-on-screen) video.
    """
    if not _wants_chinese():
        return
    try:
        from manim import config, TexTemplateLibrary
        config.tex_template = TexTemplateLibrary.ctex
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(f"[tts_service] CJK Tex template unavailable ({exc!r}); on-screen text stays English.")


def get_speech_service(**kwargs):
    """Return the speech service matching the configured narration language.

    Falls back to the English Kokoro service if the Chinese engine cannot be
    initialised (e.g. ``edge-tts`` missing or offline), so a misconfigured
    environment degrades instead of crashing the whole render.
    """
    if _wants_chinese():
        try:
            from src.utils.edge_tts_voiceover import EdgeTTSService
            return EdgeTTSService(**kwargs)
        except Exception as exc:  # pragma: no cover - defensive fallback
            print(f"[tts_service] Edge-TTS unavailable ({exc!r}), falling back to Kokoro.")

    from src.utils.kokoro_voiceover import KokoroService
    return KokoroService(**kwargs)


# Configure the CJK-capable Tex template as an import side effect, so generated
# scenes get Chinese on-screen rendering without having to remember to set it.
_configure_cjk_tex()
