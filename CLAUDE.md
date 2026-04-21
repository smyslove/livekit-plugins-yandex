# CLAUDE.md — livekit-plugins-yandex

## Проект

Кастомный плагин LiveKit Agents для Yandex SpeechKit v3 (STT + TTS).

- Python 3.10+, async/await, gRPC.
- Пакет-неймспейс `livekit.plugins.yandex` (совместим с другими `livekit-plugins-*`).
- Сборка через Hatchling, управление окружением — `uv`.

## Архитектура LiveKit плагинов

- **STT:** наследовать `livekit.agents.stt.STT`; реализовать `_recognize_impl()` (batch) и `stream()` (стриминг).
- **TTS:** наследовать `livekit.agents.tts.TTS`; реализовать `synthesize()` (ChunkedStream) и `stream()`.
- `AudioEmitter`: `initialize(...)` → `push(bytes)` → `flush()`.
- `SpeechEvent`: `INTERIM_TRANSCRIPT` и `FINAL_TRANSCRIPT`.
- До появления стриминг-STT LiveKit оборачивает batch-STT в `StreamAdapter` + Silero VAD.

## Yandex SpeechKit API

- STT gRPC endpoint: `stt.api.cloud.yandex.net:443` (v3, bidirectional stream)
- TTS gRPC endpoint: `tts.api.cloud.yandex.net:443` (v3, server stream)
- STT REST (для batch MVP): `POST https://stt.api.cloud.yandex.net/speech/v1/stt:recognize`
- Аутентификация: `Api-Key <token>` в gRPC metadata (поле `authorization`).
- Аудиоформат по умолчанию: `LINEAR16_PCM`, 8000 Hz, mono (для SIP-телефонии).
- STT стриминг: лимит сессии 5 мин, таймаут неактивности 5 с → слать `SilenceChunk`.
- TTS: лимит 250 символов на запрос → разбивка по словам/предложениям.

## Структура пакета

```
livekit/plugins/yandex/
├── __init__.py        # Экспорт STT, TTS, версии
├── stt.py             # YandexSTT + YandexRecognizeStream
├── tts.py             # YandexTTS + YandexSynthesizeStream
├── models.py          # Enum голосов, языков, мапперы
├── _auth.py           # Обёртка аутентификации, gRPC metadata
├── version.py         # __version__
├── log.py             # logger
├── py.typed           # маркер PEP 561
└── _proto/            # Сгенерированные gRPC-стабы (не править руками)
```

## gRPC-стабы

Стабы генерируются из `github.com/yandex-cloud/cloudapi` (см. `scripts/gen_proto.sh`). Никогда не редактируем `_proto/**/*_pb2.py` и `*_pb2_grpc.py` руками — перегенерируем.

## Переменные окружения

Достаточно одного из трёх способов авторизации (приоритет: IAM > OAuth > API Key):

- `YANDEX_API_KEY` — долгоживущий API-ключ сервисного аккаунта
- `YANDEX_IAM_TOKEN` — готовый IAM-токен (12 ч, без авторефреша, обновлять сам)
- `YANDEX_OAUTH_TOKEN` — OAuth-токен Yandex Passport; плагин сам обменяет его на IAM и перевыпустит по расписанию
- `YANDEX_FOLDER_ID` — folder ID, требуется для STT REST и желателен для биллинга

Настройки плагина:
- `YANDEX_TTS_VOICE` (по умолчанию `alena`)
- `YANDEX_TTS_SPEED` (по умолчанию `1.0`)
- `YANDEX_STT_MODEL` (по умолчанию `general`)
- `YANDEX_STT_LANGUAGE` (по умолчанию `ru-RU`, `auto` для автоопределения)
- `YANDEX_SAMPLE_RATE` (по умолчанию `8000`)

## Стиль кода

- Конвенции `livekit-plugins-google` / `livekit-plugins-sarvam`.
- `from __future__ import annotations` в каждом модуле.
- Обязательные type hints.
- Docstrings на английском.
- Логи — через `from .log import logger`.
- Ошибки: пробрасывать как `APIConnectionError` / `APITimeoutError` / `APIStatusError` из `livekit.agents`.

## Порядок реализации (см. план в Notion)

1. Фаза 0 — окружение, gRPC-стабы, структура.
2. Фаза 1 — TTS batch (`synthesize()` + `UtteranceSynthesis`).
3. Фаза 2 — STT batch (REST `speech/v1/stt:recognize` + `StreamAdapter`).
4. Фаза 3 — интеграция `AgentSession` + GigaChat через gpt2giga + SIP.
5. Фаза 4 — STT streaming (bidirectional gRPC, external EOU).
6. Фаза 5 — TTS streaming (`StreamSynthesis`).
7. Фаза 6 — production (retry, IAM refresh, метрики).

## Полезные ссылки

- Референс-плагины: https://github.com/livekit/agents/tree/main/livekit-plugins/
- Yandex cloudapi: https://github.com/yandex-cloud/cloudapi
- SpeechKit STT v3: https://yandex.cloud/ru/docs/speechkit/stt-v3/
- SpeechKit TTS v3: https://yandex.cloud/ru/docs/speechkit/tts-v3/
