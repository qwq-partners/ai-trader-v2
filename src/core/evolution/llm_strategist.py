"""
AI Trading Bot v2 - LLM 전략가 (LLM Strategist)

LLM을 활용하여 거래 복기 결과를 분석하고 전략 개선안을 도출합니다.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
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


def _count_trading_days(start: date, end: date) -> int:
    """실제 영업일 수 계산 (주말 + 공휴일 제외)"""
    try:
        from ..engine import is_kr_market_holiday
        count = 0
        d = start
        while d <= end:
            if d.weekday() < 5 and not is_kr_market_holiday(d):
                count += 1
            d += timedelta(days=1)
        return max(count, 1)
    except ImportError:
        # fallback: 주말만 제외
        period_days = (end - start).days or 1
        return max(period_days * 5 // 7, 1)


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

## 최우선 목표 (CRITICAL)
이 봇의 핵심 목표는 **일평균 수익률 1% 달성**입니다.
모든 분석과 파라미터 조정 제안은 이 목표 달성을 위한 것이어야 합니다.

목표 달성 기준:
- 일평균 수익률 1% 이상 → "good" (목표 달성)
- 일평균 수익률 0.5~1% → "fair" (개선 필요)
- 일평균 수익률 0.5% 미만 또는 손실 → "poor" (긴급 개선 필요)

## 분석 원칙 (우선순위 순)
1. 수익률 최적화 - 일평균 1% 수익 달성이 최우선 목표
2. 리스크 관리 - 일일 최대 손실 -2% 이내, 연속 손실 방지
3. 데이터 기반 판단 - 감정이 아닌 수치로 평가
4. 점진적 개선 - 급격한 변경보다 작은 조정
5. 실행 가능성 - 실제 적용 가능한 구체적 제안

## 전략 최적화 방향
- 승률 55% 이상 + 손익비 1.5 이상 조합으로 일 1% 도달
- 1건당 평균 수익 +2~3% / 손실 -1.5~2% 범위 유지
- 하루 2~5건 거래로 수익 분산
- 장 초반(09:00~10:00) 모멘텀 + 장중 테마 추종 병행

응답 형식:
- JSON 형식으로 구조화하여 응답
- 모든 수치는 소수점 2자리까지
- 이유와 근거를 반드시 포함
- 파라미터 조정 시 일 1% 수익률 달성에 미치는 영향을 반드시 설명"""

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
        # 분석 기간의 일수와 일평균 수익률 계산
        period_days = (review.period_end - review.period_start).days or 1
        trading_days = _count_trading_days(review.period_start.date(), review.period_end.date())
        daily_avg_return = review.total_pnl / trading_days if trading_days > 0 else 0
        daily_avg_return_pct = review.avg_pnl_pct * review.total_trades / trading_days if trading_days > 0 else 0

        prompt_parts = [
            "# 거래 복기 분석 요청",
            "",
            "## ⚠️ 최우선 목표: 일평균 수익률 1% 달성",
            f"- 분석 기간: {period_days}일 (영업일 약 {trading_days}일)",
            f"- 일평균 수익률: {daily_avg_return_pct:+.2f}% (목표: +1.00%)",
            f"- 일평균 손익금액: {daily_avg_return:+,.0f}원",
            f"- 목표 대비 달성률: {daily_avg_return_pct / 1.0 * 100:.0f}%" if daily_avg_return_pct > 0 else "- 목표 대비 달성률: 미달 (손실 구간)",
            "",
            "모든 파라미터 조정은 이 목표(일 1%)를 달성하기 위한 방향이어야 합니다.",
            "현재 목표 미달인 경우, 어떤 전략/파라미터를 변경해야 1%에 도달할 수 있는지 구체적으로 제안해주세요.",
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
            "위 데이터를 바탕으로 **일평균 1% 수익률 달성**을 최우선 목표로 삼아 분석해주세요.",
            "현재 일평균 수익률이 1% 미만이면, 1%에 도달하기 위한 구체적 방안을 제시하세요.",
            "",
            "다음을 JSON 형식으로 응답해주세요:",
            "",
            "```json",
            "{",
            '  "overall_assessment": "good(일1%이상)/fair(0.5~1%)/poor(0.5%미만) 중 하나",',
            '  "confidence_score": 0.0~1.0,',
            '  "daily_return_gap": "목표 일1% 대비 현재 부족분 분석 및 달성 방안",',
            '  "key_insights": ["인사이트1", "인사이트2", ...],',
            '  "parameter_adjustments": [',
            '    {',
            '      "parameter": "전략명.파라미터명",',
            '      "current_value": 현재값,',
            '      "suggested_value": 제안값,',
            '      "reason": "변경 이유 (일1% 달성에 미치는 영향 포함)",',
            '      "confidence": 0.0~1.0,',
            '      "expected_impact": "예상 일일 수익률 개선 효과"',
            '    }',
            '  ],',
            '  "strategy_recommendations": {',
            '    "전략명": "일1% 달성을 위한 구체적 권고"',
            '  },',
            '  "new_rules": [',
            '    {"condition": "조건", "action": "행동", "reason": "이유"}',
            '  ],',
            '  "avoid_situations": ["상황1", "상황2", ...],',
            '  "focus_opportunities": ["기회1", "기회2", ...],',
            '  "next_week_outlook": "일1% 달성을 위한 다음 주 전략 방향"',
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

    def _get_current_param(self, param_name: str, default: Any = None) -> Any:
        """현재 전략 파라미터에서 값 조회 (모든 전략에서 검색)"""
        for strategy_params in self._current_params.values():
            if param_name in strategy_params:
                return strategy_params[param_name]
        return default

    def _create_fallback_advice(self, review: ReviewResult, days: int) -> StrategyAdvice:
        """LLM 실패 시 기본 분석 결과 (일 1% 목표 기준)"""
        # 일평균 수익률 추정 (공휴일 포함)
        trading_days = _count_trading_days(review.period_start.date(), review.period_end.date())
        daily_avg_pct = review.avg_pnl_pct * review.total_trades / trading_days if trading_days > 0 else 0

        # 일 1% 목표 기준 평가
        assessment = "fair"
        if daily_avg_pct >= 1.0 and review.win_rate >= 50:
            assessment = "good"
        elif daily_avg_pct < 0.5 or review.win_rate < 40 or review.profit_factor < 1.0:
            assessment = "poor"

        insights = []
        param_adjustments = []

        # 일 1% 목표 대비 인사이트
        insights.append(
            f"일평균 수익률 {daily_avg_pct:+.2f}% (목표: +1.00%, "
            f"{'달성' if daily_avg_pct >= 1.0 else f'부족분: {1.0 - daily_avg_pct:.2f}%p'})"
        )

        # 승률 기반 인사이트 (실제 파라미터 참조)
        if review.win_rate < 40:
            insights.append(f"승률 {review.win_rate:.1f}%로 낮음 - 일 1% 달성을 위해 진입 조건 강화 필요")
            cur_min_score = self._get_current_param("min_score", 60)
            param_adjustments.append(ParameterAdjustment(
                parameter="min_score",
                current_value=cur_min_score,
                suggested_value=min(cur_min_score + 10, 90),
                reason="낮은 승률 개선을 위해 진입 기준 상향 (일 1% 달성 필수)",
                confidence=0.7,
                expected_impact="신호 수 감소, 승률 향상 → 일 수익률 개선 기대",
            ))
        elif review.win_rate >= 60:
            insights.append(f"승률 {review.win_rate:.1f}%로 양호 - 거래 빈도 증가로 일 1% 달성 가능")

        # 손익비 기반 인사이트
        if review.profit_factor < 1.0:
            insights.append(f"손익비 {review.profit_factor:.2f}로 손실 초과 - 손절 관리 시급")
            cur_sl = self._get_current_param("stop_loss_pct", 2.0)
            param_adjustments.append(ParameterAdjustment(
                parameter="stop_loss_pct",
                current_value=cur_sl,
                suggested_value=max(cur_sl - 0.5, 0.5),
                reason="손실 제한으로 손익비 개선 (일 1% 달성 전제조건)",
                confidence=0.6,
                expected_impact="개별 손실 감소 → 손익비 1.0 이상으로 개선",
            ))

        # 거래 빈도 인사이트
        daily_trades = review.total_trades / trading_days if trading_days > 0 else 0
        if daily_trades < 2:
            insights.append(f"일평균 거래 {daily_trades:.1f}건으로 적음 - 거래 기회 확대 필요")

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
            avoid_situations=[p.get("description", str(p)) for p in review.losing_patterns[:3]] if review.losing_patterns else [],
            focus_opportunities=[],
            next_week_outlook="LLM 분석 실패로 규칙 기반 분석 결과입니다. 일 1% 목표 기준 평가.",
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
