"""
AI Trading Bot v2 - LLM 전략가 (LLM Strategist)

LLM을 활용하여 거래 복기 결과를 분석하고 전략 개선안을 도출합니다.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
from loguru import logger

from .trade_journal import TradeJournal, get_trade_journal
from .trade_reviewer import TradeReviewer, ReviewResult, get_trade_reviewer
from ...utils.llm import LLMManager, LLMTask, get_llm_manager


@dataclass
class ParameterAdjustment:
    """파라미터 조정 제안"""
    parameter: str           # 파라미터 이름
    current_value: Any       # 현재 값
    suggested_value: Any     # 제안 값
    reason: str              # 변경 이유
    confidence: float        # 신뢰도 (0~1)
    expected_impact: str     # 예상 영향


@dataclass
class StrategyAdvice:
    """전략 조언"""
    # 분석 기간
    analysis_date: datetime
    period_days: int

    # 전체 평가
    overall_assessment: str      # 전반적 평가 (good/fair/poor)
    confidence_score: float      # 분석 신뢰도 (0~1)

    # 핵심 인사이트
    key_insights: List[str] = field(default_factory=list)

    # 파라미터 조정 제안
    parameter_adjustments: List[ParameterAdjustment] = field(default_factory=list)

    # 전략별 권고
    strategy_recommendations: Dict[str, str] = field(default_factory=dict)

    # 새로운 규칙 제안
    new_rules: List[Dict] = field(default_factory=list)

    # 피해야 할 상황
    avoid_situations: List[str] = field(default_factory=list)

    # 집중해야 할 기회
    focus_opportunities: List[str] = field(default_factory=list)

    # 다음 주 전망
    next_week_outlook: str = ""

    # 원본 LLM 응답
    raw_response: str = ""

    def to_dict(self) -> Dict:
        """딕셔너리로 변환"""
        return {
            "analysis_date": self.analysis_date.isoformat(),
            "period_days": self.period_days,
            "overall_assessment": self.overall_assessment,
            "confidence_score": self.confidence_score,
            "key_insights": self.key_insights,
            "parameter_adjustments": [
                {
                    "parameter": p.parameter,
                    "current_value": p.current_value,
                    "suggested_value": p.suggested_value,
                    "reason": p.reason,
                    "confidence": p.confidence,
                    "expected_impact": p.expected_impact,
                }
                for p in self.parameter_adjustments
            ],
            "strategy_recommendations": self.strategy_recommendations,
            "new_rules": self.new_rules,
            "avoid_situations": self.avoid_situations,
            "focus_opportunities": self.focus_opportunities,
            "next_week_outlook": self.next_week_outlook,
        }


class LLMStrategist:
    """
    LLM 전략가

    거래 복기 결과를 LLM에 제공하고:
    1. 성과 분석 및 평가
    2. 전략 파라미터 최적화 제안
    3. 새로운 규칙 제안
    4. 피해야 할 상황 식별
    """

    # 시스템 프롬프트
    SYSTEM_PROMPT = """당신은 경험 많은 퀀트 트레이더이자 전략 분석가입니다.
한국 주식 시장의 단기 매매 전략을 분석하고 개선안을 제시합니다.

분석 원칙:
1. 데이터 기반 판단 - 감정이 아닌 수치로 평가
2. 리스크 우선 - 손실 방지가 수익 추구보다 중요
3. 점진적 개선 - 급격한 변경보다 작은 조정
4. 실행 가능성 - 실제 적용 가능한 구체적 제안

응답 형식:
- JSON 형식으로 구조화하여 응답
- 모든 수치는 소수점 2자리까지
- 이유와 근거를 반드시 포함"""

    def __init__(
        self,
        llm_manager: LLMManager = None,
        reviewer: TradeReviewer = None,
    ):
        self.llm = llm_manager or get_llm_manager()
        self.reviewer = reviewer or get_trade_reviewer()

        # 현재 전략 파라미터 (외부에서 설정)
        self._current_params: Dict[str, Dict] = {}

    def set_current_params(self, strategy_name: str, params: Dict):
        """현재 전략 파라미터 설정"""
        self._current_params[strategy_name] = params

    async def analyze_and_advise(
        self,
        days: int = 7,
        include_parameter_suggestions: bool = True,
    ) -> StrategyAdvice:
        """
        거래 분석 및 조언 생성

        1. 복기 시스템으로 데이터 분석
        2. LLM에 분석 결과 전달
        3. 전략 개선안 수신 및 파싱
        """
        logger.info(f"[LLM 전략가] 최근 {days}일 거래 분석 시작")

        # 1. 복기 실행
        review = self.reviewer.review_period(days)

        if review.total_trades == 0:
            logger.warning("[LLM 전략가] 분석할 거래 없음")
            return StrategyAdvice(
                analysis_date=datetime.now(),
                period_days=days,
                overall_assessment="no_data",
                confidence_score=0,
                key_insights=["분석할 거래 데이터가 없습니다."],
            )

        # 2. LLM 프롬프트 구성
        prompt = self._build_analysis_prompt(review, include_parameter_suggestions)

        # 3. LLM 호출
        try:
            llm_response = await self.llm.complete(
                prompt,
                task=LLMTask.STRATEGY_ANALYSIS,
                system=self.SYSTEM_PROMPT,
            )

            if not llm_response.success or not llm_response.content:
                raise ValueError(llm_response.error or "LLM 응답 없음")

            # 4. 응답 파싱
            advice = self._parse_llm_response(llm_response.content, days)

            logger.info(
                f"[LLM 전략가] 분석 완료: 평가={advice.overall_assessment}, "
                f"인사이트 {len(advice.key_insights)}개, "
                f"파라미터 조정 {len(advice.parameter_adjustments)}개"
            )

            return advice

        except Exception as e:
            logger.error(f"[LLM 전략가] 분석 실패: {e}")

            # 폴백: 기본 분석 결과 반환
            return self._create_fallback_advice(review, days)

    def _build_analysis_prompt(
        self,
        review: ReviewResult,
        include_params: bool
    ) -> str:
        """LLM 분석 프롬프트 구성"""
        prompt_parts = [
            "# 거래 복기 분석 요청",
            "",
            review.summary_for_llm,
            "",
        ]

        # 현재 파라미터 정보
        if include_params and self._current_params:
            prompt_parts.extend([
                "## 현재 전략 파라미터",
                "",
            ])
            for strategy, params in self._current_params.items():
                prompt_parts.append(f"### {strategy}")
                for key, value in params.items():
                    prompt_parts.append(f"- {key}: {value}")
                prompt_parts.append("")

        # 전략별 성과
        if review.strategy_performance:
            prompt_parts.extend([
                "## 전략별 성과",
                "",
            ])
            for strategy, perf in review.strategy_performance.items():
                prompt_parts.append(
                    f"- {strategy}: 거래 {perf['trades']}회, "
                    f"승률 {perf.get('win_rate', 0):.1f}%, "
                    f"평균 수익률 {perf.get('avg_pnl_pct', 0):+.2f}%"
                )
            prompt_parts.append("")

        # 시간대 분석
        if review.best_entry_hours or review.worst_entry_hours:
            prompt_parts.extend([
                "## 진입 시간대 분석",
                f"- 최적 시간: {review.best_entry_hours}",
                f"- 피해야 할 시간: {review.worst_entry_hours}",
                "",
            ])

        # 분석 요청
        prompt_parts.extend([
            "## 분석 요청",
            "",
            "위 데이터를 바탕으로 다음을 JSON 형식으로 분석해주세요:",
            "",
            "```json",
            "{",
            '  "overall_assessment": "good/fair/poor 중 하나",',
            '  "confidence_score": 0.0~1.0 사이 숫자,',
            '  "key_insights": ["인사이트1", "인사이트2", ...],',
            '  "parameter_adjustments": [',
            '    {',
            '      "parameter": "파라미터명",',
            '      "current_value": 현재값,',
            '      "suggested_value": 제안값,',
            '      "reason": "변경 이유",',
            '      "confidence": 0.0~1.0,',
            '      "expected_impact": "예상 효과"',
            '    }',
            '  ],',
            '  "strategy_recommendations": {',
            '    "전략명": "권고사항"',
            '  },',
            '  "new_rules": [',
            '    {"condition": "조건", "action": "행동", "reason": "이유"}',
            '  ],',
            '  "avoid_situations": ["상황1", "상황2", ...],',
            '  "focus_opportunities": ["기회1", "기회2", ...],',
            '  "next_week_outlook": "다음 주 전망 및 전략 방향"',
            "}",
            "```",
        ])

        return "\n".join(prompt_parts)

    def _parse_llm_response(self, response: str, days: int) -> StrategyAdvice:
        """LLM 응답 파싱"""
        # JSON 추출
        json_start = response.find("{")
        json_end = response.rfind("}") + 1

        if json_start == -1 or json_end == 0:
            raise ValueError("JSON 형식 응답 없음")

        json_str = response[json_start:json_end]
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM 응답 JSON 파싱 실패: {e}")

        # ParameterAdjustment 변환
        param_adjustments = []
        for p in data.get("parameter_adjustments", []):
            param_adjustments.append(ParameterAdjustment(
                parameter=p.get("parameter", ""),
                current_value=p.get("current_value"),
                suggested_value=p.get("suggested_value"),
                reason=p.get("reason", ""),
                confidence=float(p.get("confidence", 0.5)),
                expected_impact=p.get("expected_impact", ""),
            ))

        return StrategyAdvice(
            analysis_date=datetime.now(),
            period_days=days,
            overall_assessment=data.get("overall_assessment", "fair"),
            confidence_score=float(data.get("confidence_score", 0.5)),
            key_insights=data.get("key_insights", []),
            parameter_adjustments=param_adjustments,
            strategy_recommendations=data.get("strategy_recommendations", {}),
            new_rules=data.get("new_rules", []),
            avoid_situations=data.get("avoid_situations", []),
            focus_opportunities=data.get("focus_opportunities", []),
            next_week_outlook=data.get("next_week_outlook", ""),
            raw_response=response,
        )

    def _create_fallback_advice(self, review: ReviewResult, days: int) -> StrategyAdvice:
        """LLM 실패 시 기본 분석 결과"""
        # 규칙 기반 분석
        assessment = "fair"
        if review.win_rate >= 55 and review.profit_factor >= 1.5:
            assessment = "good"
        elif review.win_rate < 40 or review.profit_factor < 1.0:
            assessment = "poor"

        insights = []
        param_adjustments = []

        # 승률 기반 인사이트
        if review.win_rate < 40:
            insights.append(f"승률 {review.win_rate:.1f}%로 낮음 - 진입 조건 강화 필요")
            param_adjustments.append(ParameterAdjustment(
                parameter="min_score",
                current_value=60,
                suggested_value=70,
                reason="낮은 승률 개선을 위해 진입 기준 상향",
                confidence=0.7,
                expected_impact="신호 수 감소, 승률 향상 기대",
            ))
        elif review.win_rate >= 60:
            insights.append(f"승률 {review.win_rate:.1f}%로 양호 - 현재 전략 유지")

        # 손익비 기반 인사이트
        if review.profit_factor < 1.0:
            insights.append(f"손익비 {review.profit_factor:.2f}로 손실 초과 - 손절 관리 필요")
            param_adjustments.append(ParameterAdjustment(
                parameter="stop_loss_pct",
                current_value=2.0,
                suggested_value=1.5,
                reason="손실 제한을 위해 손절선 타이트하게",
                confidence=0.6,
                expected_impact="개별 손실 감소, 전체 손익비 개선",
            ))

        # 패턴 기반 인사이트
        for issue in review.issues:
            insights.append(issue)

        return StrategyAdvice(
            analysis_date=datetime.now(),
            period_days=days,
            overall_assessment=assessment,
            confidence_score=0.5,  # 규칙 기반이므로 낮은 신뢰도
            key_insights=insights,
            parameter_adjustments=param_adjustments,
            strategy_recommendations={},
            new_rules=[],
            avoid_situations=review.losing_patterns[:3] if review.losing_patterns else [],
            focus_opportunities=[],
            next_week_outlook="LLM 분석 실패로 규칙 기반 분석 결과입니다.",
            raw_response="",
        )

    async def get_realtime_advice(
        self,
        symbol: str,
        current_price: float,
        indicators: Dict[str, float],
        position: Dict = None,
    ) -> str:
        """
        실시간 매매 조언

        현재 상황에서 어떻게 해야 할지 LLM에게 물어봅니다.
        """
        prompt = f"""
# 실시간 매매 조언 요청

## 종목 정보
- 종목: {symbol}
- 현재가: {current_price:,.0f}원

## 기술적 지표
"""
        for key, value in indicators.items():
            prompt += f"- {key}: {value:.2f}\n"

        if position:
            prompt += f"""
## 현재 포지션
- 보유 수량: {position.get('quantity', 0)}주
- 평균 단가: {position.get('avg_price', 0):,.0f}원
- 현재 손익: {position.get('pnl_pct', 0):+.1f}%
"""

        prompt += """
## 질문
현재 상황에서 어떤 행동을 취해야 할까요?
간단하게 한 줄로 답해주세요. (매수/매도/관망/손절/익절 중 하나와 이유)
"""

        try:
            llm_response = await self.llm.complete(
                prompt,
                task=LLMTask.QUICK_ANALYSIS,
                max_tokens=100,
            )
            if llm_response.success and llm_response.content:
                return llm_response.content.strip()
            return "분석 불가"

        except Exception as e:
            logger.error(f"실시간 조언 실패: {e}")
            return "분석 불가"


# 싱글톤 인스턴스
_llm_strategist: Optional[LLMStrategist] = None


def get_llm_strategist() -> LLMStrategist:
    """LLMStrategist 인스턴스 반환"""
    global _llm_strategist
    if _llm_strategist is None:
        _llm_strategist = LLMStrategist()
    return _llm_strategist
