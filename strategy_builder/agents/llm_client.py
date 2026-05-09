"""경량 LLM 클라이언트 — Gemini/Ollama/Claude/OpenAI 지원.

우선순위 (환경변수 설정 기준):
  1. Google Gemini — GEMINI_API_KEY 설정 시 (무료 티어 충분)
  2. Ollama       — OLLAMA_HOST에 서버 실행 중일 때 (로컬 무료)
  3. Anthropic    — ANTHROPIC_API_KEY 설정 시
  4. OpenAI       — OPENAI_API_KEY 설정 시
  5. 없으면 더미 응답

환경변수:
    GEMINI_API_KEY    Google AI Studio API 키 (무료 발급: aistudio.google.com)
    GEMINI_MODEL      Gemini 모델 (기본: gemini-1.5-flash)
    OLLAMA_HOST       Ollama 서버 주소 (기본: http://localhost:11434)
    OLLAMA_MODEL      Ollama 모델 (기본: qwen2.5:3b)
    ANTHROPIC_API_KEY Claude API 키
    OPENAI_API_KEY    OpenAI API 키
    LLM_MODEL         모델 강제 지정 (provider 무관)

Gemini 무료 티어 (2026년 기준):
    gemini-1.5-flash — 일 1,500회, 분당 15회, 입출력 100만 토큰/일 → 트레이딩 리포트 충분
    gemini-2.0-flash — 일 1,500회, 분당 15회
    API 키 발급: https://aistudio.google.com/app/apikey

⚠️ 이 클라이언트는 분석/리포트 생성 전용입니다.
   주문 실행, trading_enabled 변경, kill switch 해제는 절대 허용하지 않습니다.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-1.5-flash"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"
DEFAULT_CLAUDE_MODEL = "claude-3-5-haiku-20241022"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class LLMClient:
    """Gemini / Ollama / Claude / OpenAI 통합 LLM 클라이언트."""

    def __init__(self, model: str | None = None):
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        forced_model = model or os.environ.get("LLM_MODEL", "")

        # 우선순위 결정
        if self.gemini_key:
            self.provider = "gemini"
            self.model = forced_model or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
            logger.info("LLMClient: Google Gemini (%s)", self.model)
        elif self._ollama_available():
            self.provider = "ollama"
            self.model = forced_model or os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
            logger.info("LLMClient: Ollama (%s @ %s)", self.model, self.ollama_host)
        elif self.anthropic_key:
            self.provider = "anthropic"
            self.model = forced_model or DEFAULT_CLAUDE_MODEL
            logger.info("LLMClient: Anthropic Claude (%s)", self.model)
        elif self.openai_key:
            self.provider = "openai"
            self.model = forced_model or DEFAULT_OPENAI_MODEL
            logger.info("LLMClient: OpenAI (%s)", self.model)
        else:
            self.provider = "none"
            self.model = "none"
            logger.warning(
                "LLMClient: 사용 가능한 LLM 없음 — "
                "GEMINI_API_KEY, OLLAMA_HOST, ANTHROPIC_API_KEY, OPENAI_API_KEY 중 하나 필요"
            )

    def _ollama_available(self) -> bool:
        try:
            resp = requests.get(f"{self.ollama_host}/api/tags", timeout=3)
            return resp.ok
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self.provider != "none"

    @property
    def info(self) -> str:
        return f"{self.provider}:{self.model}"

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> tuple[str, dict[str, int]]:
        """텍스트 생성. (응답 텍스트, 토큰 사용량) 반환."""
        if not self.available:
            return (
                "[LLM 미설정 — GEMINI_API_KEY, OLLAMA_HOST, "
                "ANTHROPIC_API_KEY, OPENAI_API_KEY 중 하나 필요]",
                {},
            )
        if self.provider == "gemini":
            return self._call_gemini(system, user, max_tokens, temperature)
        if self.provider == "ollama":
            return self._call_ollama(system, user, max_tokens, temperature)
        if self.provider == "anthropic":
            return self._call_anthropic(system, user, max_tokens, temperature)
        return self._call_openai(system, user, max_tokens, temperature)

    def complete_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 2000,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """JSON 출력 강제. (dict, 토큰 사용량) 반환."""
        json_instruction = (
            "\n\n반드시 유효한 JSON만 출력하세요. "
            "코드 블록(```)이나 설명 텍스트 없이 JSON 객체 하나만 출력하세요."
        )
        text, usage = self.complete(system, user + json_instruction, max_tokens)
        try:
            cleaned = text.strip()
            if "```" in cleaned:
                parts = cleaned.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:]
                    try:
                        return json.loads(part.strip()), usage
                    except json.JSONDecodeError:
                        continue
            return json.loads(cleaned), usage
        except json.JSONDecodeError:
            logger.warning("LLM JSON 파싱 실패: %s...", text[:200])
            return {"error": "json_parse_failed", "raw": text[:500]}, usage

    # ── Google Gemini ──────────────────────────────────────────

    def _call_gemini(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, dict[str, int]]:
        """Google Gemini API 호출."""
        url = f"{GEMINI_BASE}/{self.model}:generateContent?key={self.gemini_key}"
        body = {
            "systemInstruction": {
                "parts": [{"text": system}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": user}]}
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            ],
        }
        resp = requests.post(url, json=body, timeout=60)
        if not resp.ok:
            logger.error("Gemini API 오류 %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()
        data = resp.json()

        # 응답 텍스트 추출
        candidates = data.get("candidates", [])
        if not candidates:
            logger.warning("Gemini 응답 candidates 없음: %s", data)
            return "", {}
        text = candidates[0]["content"]["parts"][0]["text"]

        # 토큰 사용량
        meta = data.get("usageMetadata", {})
        usage = {
            "prompt_tokens": meta.get("promptTokenCount", 0),
            "completion_tokens": meta.get("candidatesTokenCount", 0),
        }
        return text, usage

    def list_gemini_models(self) -> list[str]:
        """사용 가능한 Gemini 모델 목록 조회."""
        if not self.gemini_key:
            return []
        try:
            resp = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={self.gemini_key}",
                timeout=10,
            )
            if resp.ok:
                return [
                    m["name"].replace("models/", "")
                    for m in resp.json().get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                ]
        except Exception:
            pass
        return []

    # ── Ollama ────────────────────────────────────────────────

    def _call_ollama(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, dict[str, int]]:
        resp = requests.post(
            f"{self.ollama_host}/v1/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return text, {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }

    def pull_model(self, model: str | None = None) -> bool:
        """Ollama 모델 다운로드."""
        target = model or self.model
        if self.provider != "ollama":
            return False
        logger.info("Ollama 모델 다운로드 중: %s ...", target)
        try:
            resp = requests.post(
                f"{self.ollama_host}/api/pull",
                json={"name": target, "stream": False},
                timeout=600,
            )
            if resp.ok:
                logger.info("완료: %s", target)
                return True
            return False
        except Exception:
            logger.exception("Ollama pull 오류")
            return False

    def list_models(self) -> list[str]:
        """Ollama에 설치된 모델 목록."""
        if self.provider != "ollama":
            return []
        try:
            resp = requests.get(f"{self.ollama_host}/api/tags", timeout=5)
            if resp.ok:
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            pass
        return []

    # ── Anthropic Claude ─────────────────────────────────────

    def _call_anthropic(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, dict[str, int]]:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        return text, {
            "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
        }

    # ── OpenAI ───────────────────────────────────────────────

    def _call_openai(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, dict[str, int]]:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.openai_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return text, {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }
