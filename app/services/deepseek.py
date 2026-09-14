from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIRequestLog, AISettings


T = TypeVar("T", bound=BaseModel)


class DeepSeekError(RuntimeError):
    def __init__(self, message: str, *, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeepSeekUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost: float = 0.0


@dataclass(frozen=True)
class DeepSeekResult:
    data: BaseModel
    usage: DeepSeekUsage
    request_key: str
    cache_hit: bool = False


def request_key(
    company_id: str | None, operation: str, content_hash: str, missing_fields: list[str], prompt_version: str, model: str,
) -> str:
    raw = json.dumps(
        [company_id, operation, content_hash, sorted(missing_fields), prompt_version, model],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def ai_settings(session: Session) -> AISettings:
    row = session.get(AISettings, "default")
    if row is None:
        row = AISettings(id="default")
        session.add(row)
        session.flush()
    return row


def _output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text" and part.get("text"):
                text = str(part["text"]).strip()
                if text.startswith("```json") and text.endswith("```"):
                    text = text[7:-3].strip()
                elif text.startswith("```") and text.endswith("```"):
                    text = text[3:-3].strip()
                return text
    raise DeepSeekError("ИИ вернул пустой ответ.", code="empty_response")


def _human_error(exc: Exception) -> DeepSeekError:
    if isinstance(exc, DeepSeekError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return DeepSeekError("Не удалось подключиться к ИИ.", code="authentication")
        if status == 429:
            return DeepSeekError("Превышен лимит запросов к ИИ.", code="rate_limit")
        if status >= 500:
            return DeepSeekError("ИИ временно недоступен.", code="unavailable")
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return DeepSeekError("ИИ временно недоступен.", code="unavailable")
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return DeepSeekError("ИИ вернул ответ в неверном формате.", code="invalid_json")
    return DeepSeekError("Не удалось выполнить запрос к ИИ.", code="unavailable")


class DeepSeekClient:
    """Small Responses API client. The API key only comes from Settings/environment."""

    def __init__(self, session: Session, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.session = session
        self.settings = settings
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.settings.deepseek_api_key)

    def _usage(self, payload: dict[str, Any]) -> DeepSeekUsage:
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        reasoning = int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
        cost = (
            max(0, input_tokens - cached) * self.settings.deepseek_input_cost_per_million
            + cached * self.settings.deepseek_cached_input_cost_per_million
            + output_tokens * self.settings.deepseek_output_cost_per_million
        ) / 1_000_000
        return DeepSeekUsage(input_tokens, cached, output_tokens, reasoning, round(cost, 8))

    def _daily_limit_reached(self) -> bool:
        settings_row = ai_settings(self.session)
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        used = self.session.scalar(
            select(func.count()).select_from(AIRequestLog).where(
                AIRequestLog.created_at >= today,
                AIRequestLog.cache_hit.is_(False),
            )
        ) or 0
        return used >= settings_row.daily_request_limit

    def generate(
        self,
        *,
        company_id: str | None,
        operation: str,
        content_hash: str,
        missing_fields: list[str],
        prompt_version: str,
        instructions: str,
        input_text: str,
        response_model: type[T],
        schema: dict[str, Any] | None = None,
        max_output_tokens: int = 400,
        use_cache: bool = True,
        force: bool = False,
    ) -> DeepSeekResult:
        key = request_key(company_id, operation, content_hash, missing_fields, prompt_version, self.settings.deepseek_model)
        if use_cache and not force:
            cached = self.session.scalar(
                select(AIRequestLog).where(
                    AIRequestLog.request_key == key,
                    AIRequestLog.success.is_(True),
                    AIRequestLog.cache_hit.is_(False),
                ).order_by(AIRequestLog.created_at.desc()).limit(1)
            )
            if cached:
                parsed = response_model.model_validate(cached.response_data)
                self.session.add(AIRequestLog(
                    operation=operation, company_id=company_id, model=self.settings.deepseek_model,
                    request_key=key, content_hash=content_hash, missing_fields=missing_fields,
                    prompt_version=prompt_version, response_data=cached.response_data,
                    success=True, cache_hit=True,
                ))
                self.session.flush()
                return DeepSeekResult(parsed, DeepSeekUsage(), key, True)
        if not self.configured:
            raise DeepSeekError("ИИ не подключён.", code="not_configured")
        if self._daily_limit_reached():
            raise DeepSeekError("Дневной лимит запросов к ИИ достигнут.", code="daily_limit")

        output_schema = schema or response_model.model_json_schema()
        payload = {
            "model": self.settings.deepseek_model,
            "instructions": instructions,
            "input": input_text,
            "reasoning": {"effort": "none"},
            "max_output_tokens": max_output_tokens,
            "text": {"format": {"type": "json_schema", "name": operation, "schema": output_schema, "strict": True}},
            "store": False,
            "user": "leadflow",
        }
        last_error: Exception | None = None
        failed_usage = DeepSeekUsage()
        for attempt in range(3):
            try:
                with httpx.Client(transport=self.transport, timeout=90) as client:
                    response = client.post(
                        self.settings.deepseek_api_base.rstrip("/") + "/responses",
                        headers={"Authorization": f"Bearer {self.settings.deepseek_api_key}"},
                        json=payload,
                    )
                response.raise_for_status()
                body = response.json()
                failed_usage = self._usage(body)
                if body.get("status") not in {None, "completed"}:
                    raise DeepSeekError("ИИ не завершил обработку запроса.", code="incomplete")
                parsed = response_model.model_validate_json(_output_text(body))
                usage = self._usage(body)
                self.session.add(AIRequestLog(
                    operation=operation, company_id=company_id, model=str(body.get("model") or self.settings.deepseek_model),
                    request_key=key, content_hash=content_hash, missing_fields=missing_fields,
                    prompt_version=prompt_version, response_data=parsed.model_dump(mode="json"),
                    input_tokens=usage.input_tokens, cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens, reasoning_tokens=usage.reasoning_tokens,
                    estimated_cost=usage.estimated_cost, success=True, cache_hit=False,
                ))
                self.session.flush()
                return DeepSeekResult(parsed, usage, key)
            except Exception as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
                transient = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or status == 429 or status >= 500
                if not transient or attempt == 2:
                    break
                time.sleep(0.25 * (2 ** attempt))
        error = _human_error(last_error or RuntimeError("unknown"))
        self.session.add(AIRequestLog(
            operation=operation, company_id=company_id, model=self.settings.deepseek_model,
            request_key=key, content_hash=content_hash, missing_fields=missing_fields,
            prompt_version=prompt_version, input_tokens=failed_usage.input_tokens,
            cached_input_tokens=failed_usage.cached_input_tokens, output_tokens=failed_usage.output_tokens,
            reasoning_tokens=failed_usage.reasoning_tokens, estimated_cost=failed_usage.estimated_cost,
            success=False, cache_hit=False, error_code=error.code,
        ))
        self.session.flush()
        raise error
