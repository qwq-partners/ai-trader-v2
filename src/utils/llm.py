"""
AI Trading Bot v2 - LLM 통합 모듈

OpenAI와 Gemini를 통합 관리하며, 용도별 최적 모델을 자동 선택합니다.

설계 철학:
- 가벼운 작업(테마 탐지, 분류) → Gemini Flash (저비용, 빠름)
- 중요한 작업(시장 분석, 복기) → GPT-4 (깊은 분석)
- 폴백 체인: GPT-4 → GPT-3.5 → Gemini → 규칙 기반
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Callable
import aiohttp
from loguru import logger


class LLMProvider(str, Enum):
    """LLM 제공자"""
    OPENAI = "openai"
    GEMINI = "gemini"


class LLMTask(str, Enum):
    """LLM 작업 유형 - 용도별 최적 모델 자동 선택"""
    THEME_DETECTION = "theme_detection"      # 테마 탐지 → Gemini Flash
    STOCK_MAPPING = "stock_mapping"          # 종목 매핑 → Gemini Flash
    NEWS_SUMMARY = "news_summary"            # 뉴스 요약 → Gemini Flash
    MARKET_ANALYSIS = "market_analysis"      # 시장 분석 → GPT-4
    TRADE_REVIEW = "trade_review"            # 거래 복기 → GPT-4
    QUICK_CLASSIFY = "quick_classify"        # 빠른 분류 → Gemini Flash
    STRATEGY_ANALYSIS = "strategy_analysis"  # 전략 분석/진화 → GPT-4
    QUICK_ANALYSIS = "quick_analysis"        # 빠른 실시간 분석 → Gemini Flash


@dataclass
class LLMConfig:
    """LLM 설정"""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # 모델 선택
    # Heavy: 깊은 분석, 거래 복기에 사용 (GPT-5.2 Thinking 모델)
    openai_model_heavy: str = "gpt-5.2"
    # Light: 빠른 분류, 테마 탐지에 사용
    openai_model_light: str = "gpt-5-mini"
    gemini_model_heavy: str = "gemini-2.5-pro"
    gemini_model_light: str = "gemini-2.5-flash-lite"

    # 타임아웃 (Thinking 모델은 추론에 시간이 걸림)
    timeout_seconds: int = 120

    # 토큰 제한 (max_completion_tokens는 reasoning + 응답 합산)
    max_input_tokens: int = 4000
    max_output_tokens: int = 16000

    # 비용 관리
    daily_budget_usd: float = 5.0  # 일일 예산

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        )


@dataclass
class LLMUsage:
    """토큰 사용량 추적"""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def add(self, input_tokens: int, output_tokens: int, model: str):
        """사용량 추가"""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

        # 비용 추정 (대략적)
        if "gpt-4" in model:
            self.estimated_cost_usd += (input_tokens * 0.01 + output_tokens * 0.03) / 1000
        elif "gpt-3.5" in model:
            self.estimated_cost_usd += (input_tokens * 0.0005 + output_tokens * 0.0015) / 1000
        elif "gemini" in model:
            self.estimated_cost_usd += (input_tokens * 0.000075 + output_tokens * 0.0003) / 1000


@dataclass
class LLMResponse:
    """LLM 응답"""
    content: str
    model: str
    provider: LLMProvider
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    error: Optional[str] = None


def _extract_json(text: str) -> dict:
    """LLM 응답에서 JSON을 robust하게 추출"""
    text = text.strip()
    # 1. 마크다운 코드 블록 처리
    if "```" in text:
        blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)```', text)
        if blocks:
            text = blocks[0].strip()
    # 2. 첫 번째 { ... } 또는 [ ... ] 블록 추출
    if not text.startswith(("{", "[")):
        match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
        if match:
            text = match.group(1)
    return json.loads(text)


class BaseLLMClient(ABC):
    """LLM 클라이언트 기본 클래스"""

    @abstractmethod
    async def complete(self, prompt: str, system: str = "", **kwargs) -> LLMResponse:
        pass

    @abstractmethod
    async def complete_json(self, prompt: str, system: str = "", **kwargs) -> Dict[str, Any]:
        pass


class OpenAIClient(BaseLLMClient):
    """OpenAI 클라이언트"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.api_key = config.openai_api_key
        self.base_url = "https://api.openai.com/v1"
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def complete(
        self,
        prompt: str,
        system: str = "",
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """텍스트 생성"""
        if not self.api_key:
            return LLMResponse(
                content="", model="", provider=LLMProvider.OPENAI,
                success=False, error="OpenAI API key not configured"
            )

        model = model or self.config.openai_model_light
        max_tokens = max_tokens or self.config.max_output_tokens

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start_time = datetime.now()

        try:
            session = await self._get_session()

            # GPT-5 시리즈 모두 max_completion_tokens 사용 (OpenAI API v2 정책)
            # GPT-5 전체(gpt-5-mini 포함)가 temperature 커스텀 미지원 (기본값 1만 허용)
            is_gpt5 = model.startswith("gpt-5")
            is_thinking_no_temp = is_gpt5

            body = {"model": model, "messages": messages}
            # OpenAI API 최신 권장: 모든 모델에서 max_completion_tokens 사용
            body["max_completion_tokens"] = max_tokens

            if not is_thinking_no_temp:
                body["temperature"] = temperature

            async with session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                data = await resp.json()

                if resp.status != 200:
                    error_msg = data.get("error", {}).get("message", str(data))
                    return LLMResponse(
                        content="", model=model, provider=LLMProvider.OPENAI,
                        success=False, error=error_msg
                    )

                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})

                latency = (datetime.now() - start_time).total_seconds() * 1000

                return LLMResponse(
                    content=content,
                    model=model,
                    provider=LLMProvider.OPENAI,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    latency_ms=latency,
                )

        except asyncio.TimeoutError:
            return LLMResponse(
                content="", model=model, provider=LLMProvider.OPENAI,
                success=False, error="Request timeout"
            )
        except Exception as e:
            return LLMResponse(
                content="", model=model, provider=LLMProvider.OPENAI,
                success=False, error=str(e)
            )

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """JSON 응답 생성"""
        system = system or "You must respond with valid JSON only. No markdown, no explanation."
        response = await self.complete(prompt, system, **kwargs)

        if not response.success:
            return {"error": response.error}

        try:
            return _extract_json(response.content)
        except (json.JSONDecodeError, ValueError):
            return {"error": "Invalid JSON response", "raw": response.content}


class GeminiClient(BaseLLMClient):
    """Google Gemini 클라이언트"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.api_key = config.gemini_api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def complete(
        self,
        prompt: str,
        system: str = "",
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """텍스트 생성"""
        if not self.api_key:
            return LLMResponse(
                content="", model="", provider=LLMProvider.GEMINI,
                success=False, error="Gemini API key not configured"
            )

        model = model or self.config.gemini_model_light
        max_tokens = max_tokens or self.config.max_output_tokens

        # 시스템 프롬프트를 사용자 프롬프트에 통합
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        start_time = datetime.now()

        try:
            session = await self._get_session()

            url = f"{self.base_url}/models/{model}:generateContent?key={self.api_key}"

            async with session.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": full_prompt}]}],
                    "generationConfig": {
                        "temperature": temperature,
                        "maxOutputTokens": max_tokens,
                    }
                }
            ) as resp:
                data = await resp.json()

                if resp.status != 200:
                    error_msg = data.get("error", {}).get("message", str(data))
                    return LLMResponse(
                        content="", model=model, provider=LLMProvider.GEMINI,
                        success=False, error=error_msg
                    )

                # 응답 추출
                candidates = data.get("candidates", [])
                if not candidates:
                    return LLMResponse(
                        content="", model=model, provider=LLMProvider.GEMINI,
                        success=False, error="No candidates in response"
                    )

                parts = candidates[0].get("content", {}).get("parts", [])
                content = parts[0].get("text", "") if parts else ""

                # 토큰 사용량 (Gemini는 usageMetadata 제공)
                usage = data.get("usageMetadata", {})

                latency = (datetime.now() - start_time).total_seconds() * 1000

                return LLMResponse(
                    content=content,
                    model=model,
                    provider=LLMProvider.GEMINI,
                    input_tokens=usage.get("promptTokenCount", 0),
                    output_tokens=usage.get("candidatesTokenCount", 0),
                    latency_ms=latency,
                )

        except asyncio.TimeoutError:
            return LLMResponse(
                content="", model=model, provider=LLMProvider.GEMINI,
                success=False, error="Request timeout"
            )
        except Exception as e:
            return LLMResponse(
                content="", model=model, provider=LLMProvider.GEMINI,
                success=False, error=str(e)
            )

    async def complete_json(
        self,
        prompt: str,
        system: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """JSON 응답 생성"""
        system = system or "응답은 반드시 유효한 JSON 형식으로만 해주세요. 마크다운이나 설명 없이 JSON만 출력하세요."
        response = await self.complete(prompt, system, **kwargs)

        if not response.success:
            return {"error": response.error}

        try:
            return _extract_json(response.content)
        except (json.JSONDecodeError, ValueError):
            return {"error": "Invalid JSON response", "raw": response.content}


class LLMManager:
    """
    LLM 통합 관리자

    용도별 최적 모델을 자동 선택하고 폴백을 처리합니다.
    """

    # 작업별 최적 설정
    TASK_CONFIG = {
        LLMTask.THEME_DETECTION: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.STOCK_MAPPING: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.NEWS_SUMMARY: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.MARKET_ANALYSIS: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.GEMINI, "heavy"),
        },
        LLMTask.TRADE_REVIEW: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.GEMINI, "heavy"),
        },
        LLMTask.QUICK_CLASSIFY: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
        LLMTask.STRATEGY_ANALYSIS: {
            "primary": (LLMProvider.OPENAI, "heavy"),
            "fallback": (LLMProvider.GEMINI, "heavy"),
        },
        LLMTask.QUICK_ANALYSIS: {
            "primary": (LLMProvider.GEMINI, "light"),
            "fallback": (LLMProvider.OPENAI, "light"),
        },
    }

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()

        self.openai = OpenAIClient(self.config)
        self.gemini = GeminiClient(self.config)

        # 사용량 추적
        self.daily_usage = LLMUsage()
        self._usage_reset_date = datetime.now().date()

        # 통계
        self.stats = {
            "total_requests": 0,
            "success_count": 0,
            "fallback_count": 0,
            "error_count": 0,
        }

        logger.info(
            f"LLMManager 초기화: OpenAI={'설정됨' if self.config.openai_api_key else '없음'}, "
            f"Gemini={'설정됨' if self.config.gemini_api_key else '없음'}"
        )

    def _get_client(self, provider: LLMProvider) -> BaseLLMClient:
        """제공자별 클라이언트 반환"""
        if provider == LLMProvider.OPENAI:
            return self.openai
        return self.gemini

    def _get_model(self, provider: LLMProvider, weight: str) -> str:
        """제공자와 가중치에 따른 모델명 반환"""
        if provider == LLMProvider.OPENAI:
            return self.config.openai_model_heavy if weight == "heavy" else self.config.openai_model_light
        else:
            return self.config.gemini_model_heavy if weight == "heavy" else self.config.gemini_model_light

    def _check_budget(self) -> bool:
        """일일 예산 체크"""
        # 날짜가 바뀌면 리셋
        today = datetime.now().date()
        if today > self._usage_reset_date:
            self.daily_usage = LLMUsage()
            self._usage_reset_date = today

        return self.daily_usage.estimated_cost_usd < self.config.daily_budget_usd

    async def complete(
        self,
        prompt: str,
        task: LLMTask = LLMTask.QUICK_CLASSIFY,
        system: str = "",
        **kwargs
    ) -> LLMResponse:
        """
        용도에 맞는 최적 모델로 완성

        Args:
            prompt: 프롬프트
            task: 작업 유형 (자동으로 최적 모델 선택)
            system: 시스템 프롬프트
        """
        self.stats["total_requests"] += 1

        # 예산 체크
        if not self._check_budget():
            logger.warning("일일 LLM 예산 초과")
            return LLMResponse(
                content="", model="", provider=LLMProvider.OPENAI,
                success=False, error="Daily budget exceeded"
            )

        # 작업별 설정 가져오기
        task_config = self.TASK_CONFIG.get(task, self.TASK_CONFIG[LLMTask.QUICK_CLASSIFY])

        # Primary 시도
        primary_provider, primary_weight = task_config["primary"]
        primary_client = self._get_client(primary_provider)
        primary_model = self._get_model(primary_provider, primary_weight)

        response = await primary_client.complete(prompt, system, model=primary_model, **kwargs)

        if response.success:
            self.stats["success_count"] += 1
            self.daily_usage.add(response.input_tokens, response.output_tokens, response.model)
            return response

        # Fallback 시도
        logger.warning(f"Primary LLM 실패 ({primary_provider.value}): {response.error}, 폴백 시도")
        self.stats["fallback_count"] += 1

        fallback_provider, fallback_weight = task_config["fallback"]
        fallback_client = self._get_client(fallback_provider)
        fallback_model = self._get_model(fallback_provider, fallback_weight)

        response = await fallback_client.complete(prompt, system, model=fallback_model, **kwargs)

        if response.success:
            self.stats["success_count"] += 1
            self.daily_usage.add(response.input_tokens, response.output_tokens, response.model)
        else:
            self.stats["error_count"] += 1
            logger.error(f"Fallback LLM도 실패 ({fallback_provider.value}): {response.error}")

        return response

    async def complete_json(
        self,
        prompt: str,
        task: LLMTask = LLMTask.QUICK_CLASSIFY,
        system: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """JSON 응답 생성"""
        response = await self.complete(prompt, task, system, **kwargs)

        if not response.success:
            return {"error": response.error}

        try:
            return _extract_json(response.content)
        except (json.JSONDecodeError, ValueError):
            return {"error": "Invalid JSON", "raw": response.content}

    def get_usage_summary(self) -> Dict[str, Any]:
        """사용량 요약"""
        return {
            "date": str(self._usage_reset_date),
            "input_tokens": self.daily_usage.input_tokens,
            "output_tokens": self.daily_usage.output_tokens,
            "estimated_cost_usd": round(self.daily_usage.estimated_cost_usd, 4),
            "budget_remaining_usd": round(self.config.daily_budget_usd - self.daily_usage.estimated_cost_usd, 4),
            "stats": self.stats,
        }


# 전역 인스턴스 (선택적 사용)
_llm_manager: Optional[LLMManager] = None

def get_llm_manager() -> LLMManager:
    """전역 LLM 매니저 인스턴스"""
    global _llm_manager
    if _llm_manager is None:
        _llm_manager = LLMManager()
    return _llm_manager
