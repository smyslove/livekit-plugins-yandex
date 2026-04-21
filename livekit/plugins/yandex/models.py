"""Voices, languages and constants for Yandex SpeechKit v3."""

from __future__ import annotations

from typing import Literal

# Default gRPC endpoints for Yandex SpeechKit v3.
STT_GRPC_ENDPOINT = "stt.api.cloud.yandex.net:443"
TTS_GRPC_ENDPOINT = "tts.api.cloud.yandex.net:443"

# REST endpoint used by the batch STT MVP (POST <base>/speech/v1/stt:recognize).
STT_REST_BASE = "https://stt.api.cloud.yandex.net"


# Voices available for ресторанный консьерж use case.
# See https://yandex.cloud/ru/docs/speechkit/tts/voices
TTSVoice = Literal[
    "alena",    # ru, female, friendly — рекомендуемый голос
    "marina",   # ru, female
    "filipp",   # ru, male
    "jane",     # ru, female
    "omazh",    # ru, female
    "zahar",    # ru, male
    "ermil",    # ru, male
    "madirus",  # ru, male
    "john",     # en, male
    "nigora",   # uz, female
    "lera",     # ru, female
]


# Roles Yandex supports for some voices (e.g. "neutral", "friendly", "good").
TTSRole = Literal[
    "neutral",
    "good",
    "evil",
    "friendly",
    "strict",
    "whisper",
]


# Supported STT languages (BCP-47).
# Source: https://yandex.cloud/ru/docs/speechkit/stt/models
STTLanguage = Literal[
    "auto",   # автоопределение
    "ru-RU",
    "en-US",
    "tr-TR",
    "kk-KZ",
    "uz-UZ",
    "de-DE",
    "fr-FR",
    "nl-NL",
    "it-IT",
    "pl-PL",
    "he-HE",
    "pt-BR",
]


# TTS v3 supports fewer languages (ru, en, tr). BCP-47 is not required by Yandex for TTS
# (voice itself encodes the language), but we expose the hint for consistency.
TTSLanguage = Literal[
    "ru-RU",
    "en-US",
    "tr-TR",
]


# STT recognition models.
STTModel = Literal[
    "general",
    "general:rc",
    "general:deprecated",
]


# Default PCM sample rate for SIP telephony.
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_NUM_CHANNELS = 1

# TTS v3 utterance synthesis character limit.
# Beyond this, either chunk the text or enable `unsafe_mode` (bills per 250-char unit).
TTS_MAX_CHARS_PER_REQUEST = 250

# STT streaming constraints.
STT_SESSION_MAX_DURATION_S = 5 * 60   # 5 minutes
STT_INACTIVITY_TIMEOUT_S = 5          # 5 seconds of silence triggers keep-alive
