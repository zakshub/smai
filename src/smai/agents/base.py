"""Unified LLM client and structured agent runner."""

from __future__ import annotations

import json
import re
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from smai.config import Settings
from smai.db.database import Database


T = TypeVar("T", bound=BaseModel)


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    return text


class LLMResponse(BaseModel):
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = "mock"


class LLMClient:
    """Provider-agnostic LLM client supporting Gemini, OpenAI, Anthropic, Ollama, and Mock."""

    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self.settings = settings
        self.db = db

    @property
    def provider(self) -> str:
        return self.settings.smai_llm_provider.lower().strip()

    @property
    def model(self) -> str:
        if self.provider == "mock":
            return "mock-local"
        return self.settings.smai_llm_model

    async def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
        fallback_factory: Any = None,
        draft_id: int | None = None,
    ) -> T:
        started = time.perf_counter()
        missing_gemini_key = self.provider == "gemini" and not self.settings.effective_gemini_key()
        if (self.provider == "mock" or missing_gemini_key) and fallback_factory is not None:
            result = fallback_factory()
            latency = int((time.perf_counter() - started) * 1000)
            if self.db:
                self.db.log_agent_run(
                    agent_name=agent_name,
                    model=self.settings.smai_llm_model if self.provider == "gemini" else "mock-local",
                    status="ok",
                    latency_ms=latency,
                    draft_id=draft_id,
                )
            return result

        try:
            resp = await self._call_provider(system_prompt, user_prompt)
            cleaned = _strip_json_fences(resp.text)
            data = json.loads(cleaned)
            parsed = schema.model_validate(data)
            latency = int((time.perf_counter() - started) * 1000)
            if self.db:
                self.db.log_agent_run(
                    agent_name=agent_name,
                    model=resp.model,
                    status="ok",
                    latency_ms=latency,
                    tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out,
                    draft_id=draft_id,
                )
            return parsed
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            if self.db:
                self.db.log_agent_run(
                    agent_name=agent_name,
                    model=self.model,
                    status="error",
                    latency_ms=latency,
                    draft_id=draft_id,
                    error_message=str(exc),
                )
            if fallback_factory is not None:
                return fallback_factory()
            raise

    async def _call_provider(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        provider = self.provider
        if provider == "gemini":
            return await self._call_gemini(system_prompt, user_prompt)
        if provider == "openai":
            return await self._call_openai(system_prompt, user_prompt)
        if provider == "anthropic":
            return await self._call_anthropic(system_prompt, user_prompt)
        if provider == "ollama":
            return await self._call_ollama(system_prompt, user_prompt)
        raise ValueError(f"Unsupported LLM provider: {provider}")

    async def _call_gemini(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        api_key = self.settings.effective_gemini_key()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.smai_llm_model}:generateContent?key={api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(url, json=payload)
            res.raise_for_status()
            body = res.json()
        text = body["candidates"][0]["content"]["parts"][0]["text"]
        usage = body.get("usageMetadata", {})
        return LLMResponse(
            text=text,
            tokens_in=int(usage.get("promptTokenCount", 0)),
            tokens_out=int(usage.get("candidatesTokenCount", 0)),
            model=self.settings.smai_llm_model,
        )

    async def _call_openai(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        api_key = self.settings.openai_api_key
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {
            "model": self.settings.smai_llm_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(url, headers=headers, json=payload)
            res.raise_for_status()
            body = res.json()
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
        return LLMResponse(
            text=text,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            model=self.settings.smai_llm_model,
        )

    async def _call_anthropic(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        api_key = self.settings.anthropic_api_key
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.settings.smai_llm_model,
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(url, headers=headers, json=payload)
            res.raise_for_status()
            body = res.json()
        text = body["content"][0]["text"]
        usage = body.get("usage", {})
        return LLMResponse(
            text=text,
            tokens_in=int(usage.get("input_tokens", 0)),
            tokens_out=int(usage.get("output_tokens", 0)),
            model=self.settings.smai_llm_model,
        )

    async def _call_ollama(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.settings.smai_llm_model,
            "format": "json",
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=90.0) as client:
            res = await client.post(url, json=payload)
            res.raise_for_status()
            body = res.json()
        text = body["message"]["content"]
        return LLMResponse(text=text, model=self.settings.smai_llm_model)
