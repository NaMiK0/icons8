"""Клиент к OpenAI-совместимому API.

Намеренно на стандартной библиотеке: proxyAPI, OpenRouter, локальная
Ollama и сам OpenAI говорят одним протоколом, а POST с JSON не стоит
отдельного пакета в зависимостях. Меньше ставить на чужой машине — меньше
шансов, что запуск на созвоне начнётся с разбора чужого окружения.

Ответ запрашивается в режиме json_object, а не strict json_schema:
на проверке qwen3.5-27b строгая схема давала валидный, но заметно худший
по смыслу ответ — модель отвечала «keep: true» почти на всё. Свободный
JSON плюс собственная валидация оказались точнее.
"""

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .cache import ResponseCache, cache_key

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.proxyapi.ru/v1"
DEFAULT_TIMEOUT = 300
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})

#: Сколько раз переспросить, если модель ответила мусором вместо JSON.
#: Не теоретическая предосторожность: qwen3.5-27b при temperature 0 иногда
#: возвращает литерал `null` вместо объекта — на одной и той же пачке из 25
#: запросов, тогда как на 50 отвечает нормально.
JSON_ATTEMPTS = 3

RETRY_HINT = (
    "Your previous reply was not a valid JSON object of the requested shape. "
    "Reply again with the JSON object only: no prose, no code fences, no null."
)


class LLMError(Exception):
    """Обращение к модели не удалось — стадия уходит в офлайн-фолбэк."""


@dataclass(slots=True)
class Usage:
    """Расход токенов по стадии — попадает в отчёт."""

    stage: str
    model: str
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    seconds: float = 0.0

    def add(self, usage: dict[str, Any], seconds: float) -> None:
        self.calls += 1
        self.input_tokens += usage.get("prompt_tokens", 0)
        self.output_tokens += usage.get("completion_tokens", 0)
        details = usage.get("completion_tokens_details") or {}
        self.reasoning_tokens += details.get("reasoning_tokens", 0)
        self.seconds += seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "model": self.model,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "seconds": round(self.seconds, 1),
        }


@dataclass(slots=True)
class LLMConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    temperature: float | None = None
    max_retries: int = 4
    timeout: int = DEFAULT_TIMEOUT
    reasoning_effort: str | None = None
    max_tokens: int = 16000

    @classmethod
    def from_env(cls, settings=None) -> "LLMConfig":
        """Ключ и адрес — из окружения, режимы — из config.toml."""
        get = settings.get if settings else (lambda *_args, **_kw: None)
        reasoning = os.environ.get("LLM_REASONING_EFFORT") or get("llm.reasoning_effort")
        temperature = get("llm.temperature")
        return cls(
            base_url=os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL,
            api_key=os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "",
            temperature=float(temperature) if temperature is not None else None,
            max_retries=int(get("llm.max_retries") or 4),
            timeout=int(get("llm.timeout") or DEFAULT_TIMEOUT),
            reasoning_effort=reasoning or None,
            max_tokens=int(get("llm.max_tokens") or 16000),
        )


@dataclass(slots=True)
class LLMClient:
    """Минимальный клиент chat/completions с кэшем и повторами."""

    config: LLMConfig
    cache: ResponseCache = field(default_factory=ResponseCache)
    usage: dict[str, Usage] = field(default_factory=dict)
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """Чем отправлять запрос. По умолчанию — HTTP; подменяется в тестах."""

    @property
    def available(self) -> bool:
        return bool(self.config.api_key)

    def complete_json(
        self,
        *,
        stage: str,
        model: str,
        system: str,
        user: str,
        validate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Запросить JSON-ответ, переспросив при негодном.

        `validate` проверяет форму ответа глазами стадии: клиенту неизвестно,
        что именно она ждёт, а различать «модель ответила» и «модель ответила
        осмысленно» нужно до того, как ответ ляжет в кэш.
        """
        payload = self._payload(model, system, user)
        key = cache_key(payload)
        tracker = self.usage.setdefault(stage, Usage(stage=stage, model=model))

        cached = self.cache.get(key)
        if cached is not None:
            tracker.cache_hits += 1
            log.debug("стадия %s: ответ взят из кэша", stage)
            return cached

        last_error: LLMError | None = None
        for attempt in range(1, JSON_ATTEMPTS + 1):
            request = payload if attempt == 1 else self._payload(
                model, system, f"{user}\n\n{RETRY_HINT}"
            )
            raw = (self.transport or self._request)(request)
            tracker.add(raw.get("usage") or {}, raw.get("_seconds", 0.0))

            try:
                parsed = _parse_json(_extract_content(raw))
                if validate and not validate(parsed):
                    raise LLMError("ответ не той формы, какую ждёт стадия")
            except LLMError as error:
                last_error = error
                log.warning("стадия %s: попытка %s — %s", stage, attempt, error)
                continue

            self.cache.put(key, parsed)
            return parsed

        raise LLMError(f"негодный ответ после {JSON_ATTEMPTS} попыток: {last_error}")

    def _payload(self, model: str, system: str, user: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        return payload

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            raise LLMError("не задан LLM_API_KEY")

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        context = _ssl_context()
        last_error: Exception | None = None

        for attempt in range(1, self.config.max_retries + 1):
            request = urllib.request.Request(url, data=body, headers=headers)
            started = time.monotonic()
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout, context=context
                ) as response:
                    data = json.load(response)
                data["_seconds"] = time.monotonic() - started
                return data
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:300]
                last_error = LLMError(f"HTTP {error.code}: {detail}")
                if error.code not in RETRY_STATUSES:
                    raise last_error from error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = LLMError(f"{type(error).__name__}: {error}")

            if attempt < self.config.max_retries:
                pause = min(2 ** attempt, 30)
                log.warning("попытка %s не удалась (%s), повтор через %s c",
                            attempt, last_error, pause)
                time.sleep(pause)

        raise LLMError(f"не удалось получить ответ за {self.config.max_retries} попыток: {last_error}")


def _ssl_context() -> ssl.SSLContext:
    """Сборки python.org на macOS приходят без корневых сертификатов.

    Если в окружении есть certifi — берём его хранилище, иначе системное.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - зависит от окружения
        return ssl.create_default_context()


def _extract_content(response: dict[str, Any]) -> str:
    try:
        return response["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as error:
        raise LLMError(f"неожиданная форма ответа: {error}") from error


def _parse_json(content: str) -> dict[str, Any]:
    """Разобрать ответ, простив модели обёртку из ```json."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    if not text:
        raise LLMError("модель вернула пустой ответ")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise LLMError(f"ответ не является JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise LLMError("ожидался JSON-объект")
    return parsed
