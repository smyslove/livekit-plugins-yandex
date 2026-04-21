"""LiveKit Agents plugin for Yandex SpeechKit v3.

Provides STT (batch via REST, streaming via gRPC in phase 4) and
TTS (batch via gRPC `UtteranceSynthesis`, streaming in phase 5).
"""

from __future__ import annotations

from livekit.agents import Plugin

from .log import logger
from .stt import STT, SpeechStream
from .tts import TTS, SynthesizeStream
from .version import __version__

__all__ = [
    "STT",
    "TTS",
    "SpeechStream",
    "SynthesizeStream",
    "__version__",
]


class YandexPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(__name__, __version__, __package__, logger)


Plugin.register_plugin(YandexPlugin())

# Hide internal modules from pdoc.
_module = dir()
NOT_IN_ALL = [m for m in _module if m not in __all__]
__pdoc__ = {n: False for n in NOT_IN_ALL}
