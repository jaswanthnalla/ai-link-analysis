"""Step 6 of the processing flow: the only module that talks to the LLM API.

The provider is Google Gemini, called through the official ``google-genai`` SDK. The primary model
is tried first. If the call fails (API error, timeout, refusal, truncated or schema-invalid output)
the same request is sent once to the fallback model, unless the failure is one the fallback cannot
fix (a rejected or missing API key). Structured JSON is enforced with Gemini structured outputs: a
Pydantic model is passed as the response schema and the SDK returns the parsed object.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.schemas import InvalidLLMOutputError, LLMServiceError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Settings value (low | medium | high) mapped to the Gemini thinking level.
_THINKING_LEVELS = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}
# Finish reasons that mean the model refused or was blocked rather than answering.
_REFUSAL_FINISH_REASONS = frozenset(
    {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"}
)


@dataclass
class LLMCallResult(Generic[T]):
    """Parsed output plus the usage figures needed for cost and latency reporting."""

    output: T
    model: str
    used_fallback: bool
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    elapsed_seconds: float
    response_id: str


class LLMService:
    """Thin wrapper around the Google Gemini API with primary/fallback handling."""

    def __init__(self, settings: Settings | None = None, client: genai.Client | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def client(self) -> genai.Client:
        # Created lazily so the application (and its tests) can start without an API key.
        if self._client is None:
            if not self.settings.google_api_key:
                raise LLMServiceError("The Gemini API key is not configured (set GOOGLE_API_KEY).", retryable=False)
            self._client = genai.Client(
                api_key=self.settings.google_api_key,
                # The preview model (and the Gemini 3 family) is served on v1beta, not the default v1.
                http_options=genai_types.HttpOptions(
                    timeout=int(self.settings.llm_timeout_seconds * 1000),
                    api_version="v1beta",
                ),
            )
        return self._client

    def _models(self) -> list[str]:
        models = [self.settings.llm_primary_model]
        fallback = self.settings.llm_fallback_model
        if fallback and fallback != self.settings.llm_primary_model:
            models.append(fallback)
        return models

    def _thinking_level(self) -> str:
        return _THINKING_LEVELS.get(self.settings.llm_reasoning_effort.strip().lower(), "MEDIUM")

    async def generate(self, *, instructions: str, user_input: str, output_model: type[T]) -> LLMCallResult[T]:
        """Run the request on the primary model, falling back once to the fallback model."""
        if self._client is None and not self.settings.google_api_key:
            raise LLMServiceError("The Gemini API key is not configured (set GOOGLE_API_KEY).", retryable=False)
        last_error: LLMServiceError | None = None
        for index, model in enumerate(self._models()):
            try:
                return await self._call(model, instructions, user_input, output_model, used_fallback=index > 0)
            except LLMServiceError as exc:
                last_error = exc
                logger.warning("LLM call failed on %s (%s): %s", model, exc.error_type, exc.message)
                if not exc.retryable:
                    break
        assert last_error is not None
        raise last_error

    async def _call(
        self,
        model: str,
        instructions: str,
        user_input: str,
        output_model: type[T],
        *,
        used_fallback: bool,
    ) -> LLMCallResult[T]:
        started = time.perf_counter()
        # response_json_schema (not response_schema) is used because the models set extra="forbid",
        # which puts additionalProperties in the schema; the response_schema field rejects that key.
        config = genai_types.GenerateContentConfig(
            system_instruction=instructions,
            max_output_tokens=self.settings.llm_max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=output_model.model_json_schema(),
            thinking_config=genai_types.ThinkingConfig(thinking_level=self._thinking_level()),
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=model,
                contents=user_input,
                config=config,
            )
        except genai_errors.ClientError as exc:
            raise self._map_client_error(exc, model) from None
        except genai_errors.ServerError as exc:
            logger.warning("LLM API error model=%s status=%s message=%s", model, exc.code, _safe(exc.message))
            raise LLMServiceError(f"The Gemini API returned HTTP {exc.code} for {model}.") from None
        except genai_errors.APIError as exc:
            logger.warning("LLM API error model=%s status=%s message=%s", model, exc.code, _safe(exc.message))
            raise LLMServiceError(f"The Gemini API call failed for {model} (HTTP {exc.code}).") from None
        except ValidationError as exc:
            raise InvalidLLMOutputError(
                f"The output of {model} did not match the expected schema: {_summarize_validation_error(exc)}"
            ) from None
        except Exception as exc:  # transport errors, timeouts and anything the SDK does not classify
            name = exc.__class__.__name__
            if "timeout" in name.lower():
                raise LLMServiceError(
                    f"The Gemini request to {model} did not complete within "
                    f"{self.settings.llm_timeout_seconds:g} seconds."
                ) from None
            raise LLMServiceError(f"The Gemini client failed ({name}).") from None

        elapsed = time.perf_counter() - started

        finish_reason = self._finish_reason(response)
        if finish_reason == "MAX_TOKENS":
            raise InvalidLLMOutputError(
                f"{model} returned truncated or invalid JSON; the output hit the "
                f"{self.settings.llm_max_output_tokens}-token limit."
            )
        if finish_reason in _REFUSAL_FINISH_REASONS:
            raise LLMServiceError(f"{model} declined to analyze this content (finish reason: {finish_reason}).")

        parsed = output_model.model_validate(response.parsed) if isinstance(response.parsed, dict) else response.parsed
        if parsed is None:
            raise InvalidLLMOutputError(f"{model} returned no structured output.")
        if not isinstance(parsed, output_model):
            raise InvalidLLMOutputError(f"{model} returned no structured output of the expected type.")

        usage = response.usage_metadata
        result = LLMCallResult(
            output=parsed,
            model=getattr(response, "model_version", None) or model,
            used_fallback=used_fallback,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            cached_input_tokens=getattr(usage, "cached_content_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            reasoning_tokens=getattr(usage, "thoughts_token_count", 0) or 0,
            total_tokens=getattr(usage, "total_token_count", 0) or 0,
            elapsed_seconds=elapsed,
            response_id=getattr(response, "response_id", "") or "",
        )
        logger.info(
            "LLM call ok model=%s fallback=%s input_tokens=%d cached_input_tokens=%d output_tokens=%d "
            "reasoning_tokens=%d elapsed=%.2fs",
            result.model,
            result.used_fallback,
            result.input_tokens,
            result.cached_input_tokens,
            result.output_tokens,
            result.reasoning_tokens,
            result.elapsed_seconds,
        )
        return result

    def _map_client_error(self, exc: genai_errors.ClientError, model: str) -> LLMServiceError:
        logger.warning("LLM API error model=%s status=%s message=%s", model, exc.code, _safe(exc.message))
        message = _safe(exc.message or exc.status or str(exc))
        lowered = message.lower()
        if exc.code in (401, 403) or "api key" in lowered or "api_key" in lowered or "permission" in lowered:
            return LLMServiceError("The Gemini API rejected the configured API key.", retryable=False)
        if exc.code == 429:
            return LLMServiceError(f"The Gemini API rate limit was reached for {model}.")
        return LLMServiceError(f"The Gemini API returned HTTP {exc.code} for {model}.")

    @staticmethod
    def _finish_reason(response: object) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        raw = getattr(candidates[0], "finish_reason", None)
        if raw is None:
            return None
        # The SDK uses a str-enum; normalise both plain strings and enum members.
        name = getattr(raw, "name", None) or str(raw)
        return name.split(".")[-1].upper()


def _safe(text: object, limit: int = 200) -> str:
    cleaned = " ".join(_CONTROL_RE.sub("", str(text or "")).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


def _summarize_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        location = ".".join(str(p) for p in error.get("loc", ())) or "root"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)
