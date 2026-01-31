"""
AI Trading Bot v2 - 전략 진화기 (Strategy Evolver)

LLM의 조언을 실제 전략에 반영하고, 성과를 추적합니다.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from loguru import logger

from .trade_journal import get_trade_journal
from .trade_reviewer import get_trade_reviewer, ReviewResult
from .llm_strategist import (
    LLMStrategist, StrategyAdvice, ParameterAdjustment, get_llm_strategist
)


@dataclass
class ParameterChange:
    """파라미터 변경 기록"""
    timestamp: datetime
    strategy: str
    parameter: str
    old_value: Any
    new_value: Any
    reason: str
    source: str  # "llm" or "manual" or "rollback"

    # 성과 추적
    trades_before: int = 0
    win_rate_before: float = 0
    trades_after: int = 0
    win_rate_after: float = 0
    is_effective: Optional[bool] = None

    def to_dict(self) -> Dict:
        return {
            **asdict(self),
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class EvolutionState:
    """진화 상태"""
    version: int = 1
    last_evolution: Optional[datetime] = None
    total_evolutions: int = 0
    successful_changes: int = 0
    rolled_back_changes: int = 0

    # 현재 적용된 변경 사항
    active_changes: List[ParameterChange] = field(default_factory=list)

    # 변경 이력
    change_history: List[ParameterChange] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "version": self.version,
            "last_evolution": self.last_evolution.isoformat() if self.last_evolution else None,
            "total_evolutions": self.total_evolutions,
            "successful_changes": self.successful_changes,
            "rolled_back_changes": self.rolled_back_changes,
            "active_changes": [c.to_dict() for c in self.active_changes],
            "change_history": [c.to_dict() for c in self.change_history[-100:]],  # 최근 100개
        }


class StrategyEvolver:
    """
    전략 진화기

    LLM 조언을 바탕으로:
    1. 전략 파라미터 자동 조정
    2. 변경 효과 추적
    3. 비효율적인 변경 롤백
    4. 진화 이력 관리
    """

    def __init__(
        self,
        llm_strategist: LLMStrategist = None,
        storage_dir: str = None,
    ):
        self.strategist = llm_strategist or get_llm_strategist()
        self.reviewer = get_trade_reviewer()
        self.journal = get_trade_journal()

        # 저장소
        self.storage_dir = Path(storage_dir or os.getenv(
            "EVOLUTION_DIR",
            os.path.expanduser("~/.cache/ai_trader/evolution")
        ))
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # 상태
        self.state = self._load_state()

        # 전략 참조 (외부에서 설정)
        self._strategies: Dict[str, Any] = {}  # name -> strategy object
        self._param_setters: Dict[str, Callable] = {}  # "strategy.param" -> setter function

        # 설정
        self.min_trades_for_evaluation = 10  # 평가에 필요한 최소 거래 수
        self.evaluation_period_days = 7      # 변경 효과 평가 기간
        self.min_confidence_to_apply = 0.6   # 적용 최소 신뢰도
        self.auto_rollback_threshold = -5.0  # 승률 감소 시 롤백 임계값

        logger.info(f"StrategyEvolver 초기화: {self.storage_dir}")

    def _load_state(self) -> EvolutionState:
        """진화 상태 로드"""
        state_file = self.storage_dir / "evolution_state.json"

        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                state = EvolutionState(
                    version=data.get("version", 1),
                    last_evolution=datetime.fromisoformat(data["last_evolution"]) if data.get("last_evolution") else None,
                    total_evolutions=data.get("total_evolutions", 0),
                    successful_changes=data.get("successful_changes", 0),
                    rolled_back_changes=data.get("rolled_back_changes", 0),
                )

                logger.info(f"진화 상태 로드: v{state.version}, 총 {state.total_evolutions}회 진화")
                return state

            except Exception as e:
                logger.warning(f"진화 상태 로드 실패: {e}")

        return EvolutionState()

    def _save_state(self):
        """진화 상태 저장"""
        state_file = self.storage_dir / "evolution_state.json"

        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(self.state.to_dict(), f, ensure_ascii=False, indent=2)

    def register_strategy(
        self,
        name: str,
        strategy: Any,
        param_setters: Dict[str, Callable] = None,
    ):
        """
        전략 등록

        Args:
            name: 전략 이름
            strategy: 전략 객체
            param_setters: 파라미터 설정 함수 맵 {"param_name": setter_func}
        """
        self._strategies[name] = strategy
        if param_setters:
            for param, setter in param_setters.items():
                self._param_setters[f"{name}.{param}"] = setter

        # LLM 전략가에 현재 파라미터 전달
        if hasattr(strategy, 'config'):
            config = strategy.config
            params = {
                k: getattr(config, k)
                for k in dir(config)
                if not k.startswith('_') and not callable(getattr(config, k))
            }
            self.strategist.set_current_params(name, params)

        logger.info(f"전략 등록: {name}")

    async def evolve(
        self,
        days: int = 7,
        dry_run: bool = False,
    ) -> StrategyAdvice:
        """
        전략 진화 실행

        1. LLM에게 분석 요청
        2. 조언 검토 및 필터링
        3. 파라미터 적용 (dry_run=False인 경우)
        4. 상태 업데이트

        Args:
            days: 분석 기간 (일)
            dry_run: True면 실제 적용 없이 조언만 반환
        """
        logger.info(f"[진화] 최근 {days}일 분석 및 진화 시작 (dry_run={dry_run})")

        # 1. LLM 분석
        advice = await self.strategist.analyze_and_advise(days)

        if advice.overall_assessment == "no_data":
            logger.warning("[진화] 분석할 데이터 없음")
            return advice

        # 2. 현재 성과 기록 (변경 전)
        current_review = self.reviewer.review_period(days)

        # 3. 파라미터 조정 검토 및 적용
        applied_changes = []

        for adjustment in advice.parameter_adjustments:
            # 신뢰도 체크
            if adjustment.confidence < self.min_confidence_to_apply:
                logger.debug(
                    f"[진화] {adjustment.parameter} 스킵 "
                    f"(신뢰도 {adjustment.confidence:.2f} < {self.min_confidence_to_apply})"
                )
                continue

            # 파라미터 키 파싱 (strategy.param 형식)
            param_key = self._find_param_key(adjustment.parameter)
            if not param_key:
                logger.warning(f"[진화] 알 수 없는 파라미터: {adjustment.parameter}")
                continue

            # dry_run이 아니면 실제 적용
            if not dry_run:
                success = self._apply_parameter_change(
                    param_key,
                    adjustment.current_value,
                    adjustment.suggested_value,
                    adjustment.reason,
                    current_review,
                )
                if success:
                    applied_changes.append(adjustment)
            else:
                logger.info(
                    f"[진화][DRY] {param_key}: {adjustment.current_value} -> "
                    f"{adjustment.suggested_value} ({adjustment.reason})"
                )

        # 4. 상태 업데이트
        if applied_changes and not dry_run:
            self.state.total_evolutions += 1
            self.state.last_evolution = datetime.now()
            self.state.version += 1
            self._save_state()

            logger.info(f"[진화] 완료: {len(applied_changes)}개 파라미터 변경")

        # 5. 조언 로깅
        self._log_advice(advice)

        return advice

    def _find_param_key(self, param_name: str) -> Optional[str]:
        """파라미터 키 찾기"""
        # 이미 "strategy.param" 형식이면 그대로 반환
        if "." in param_name:
            if param_name in self._param_setters:
                return param_name
            return None

        # 등록된 모든 전략에서 찾기
        for strategy_name in self._strategies:
            full_key = f"{strategy_name}.{param_name}"
            if full_key in self._param_setters:
                return full_key

            # config에서 찾기
            strategy = self._strategies[strategy_name]
            if hasattr(strategy, 'config') and hasattr(strategy.config, param_name):
                return full_key

        return None

    def _apply_parameter_change(
        self,
        param_key: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        current_review: ReviewResult,
    ) -> bool:
        """파라미터 변경 적용"""
        try:
            strategy_name, param_name = param_key.split(".", 1)

            # setter 함수가 있으면 사용
            if param_key in self._param_setters:
                self._param_setters[param_key](new_value)
            # config 직접 수정
            elif strategy_name in self._strategies:
                strategy = self._strategies[strategy_name]
                if hasattr(strategy, 'config') and hasattr(strategy.config, param_name):
                    setattr(strategy.config, param_name, new_value)
                else:
                    return False
            else:
                return False

            # 변경 기록
            change = ParameterChange(
                timestamp=datetime.now(),
                strategy=strategy_name,
                parameter=param_name,
                old_value=old_value,
                new_value=new_value,
                reason=reason,
                source="llm",
                trades_before=current_review.total_trades,
                win_rate_before=current_review.win_rate,
            )

            self.state.active_changes.append(change)
            self.state.change_history.append(change)

            logger.info(
                f"[진화] 파라미터 변경: {param_key} = {old_value} -> {new_value} "
                f"(사유: {reason})"
            )

            return True

        except Exception as e:
            logger.error(f"[진화] 파라미터 변경 실패: {param_key} - {e}")
            return False

    async def evaluate_changes(self) -> List[Dict]:
        """
        변경 효과 평가

        적용된 변경 사항들의 효과를 평가하고,
        비효율적인 변경은 롤백합니다.
        """
        results = []

        for change in self.state.active_changes[:]:  # 복사본으로 순회
            # 평가 기간 체크
            days_since = (datetime.now() - change.timestamp).days
            if days_since < self.evaluation_period_days:
                continue

            # 현재 성과 조회
            strategy_trades = self.journal.get_trades_by_strategy(
                change.strategy,
                days=self.evaluation_period_days
            )

            if len(strategy_trades) < self.min_trades_for_evaluation:
                logger.debug(f"[진화] {change.parameter} 평가 대기 중 (거래 부족)")
                continue

            # 승률 계산
            wins = len([t for t in strategy_trades if t.is_win])
            current_win_rate = wins / len(strategy_trades) * 100 if strategy_trades else 0

            change.trades_after = len(strategy_trades)
            change.win_rate_after = current_win_rate

            # 효과 판단
            win_rate_diff = current_win_rate - change.win_rate_before

            if win_rate_diff >= 0:
                change.is_effective = True
                self.state.successful_changes += 1
                result_str = "효과적"
            elif win_rate_diff <= self.auto_rollback_threshold:
                # 롤백 실행
                change.is_effective = False
                await self._rollback_change(change)
                result_str = "롤백됨"
            else:
                change.is_effective = False
                result_str = "비효율적"

            # 결과 기록
            result = {
                "parameter": f"{change.strategy}.{change.parameter}",
                "old_value": change.old_value,
                "new_value": change.new_value,
                "win_rate_before": change.win_rate_before,
                "win_rate_after": current_win_rate,
                "win_rate_diff": win_rate_diff,
                "result": result_str,
            }
            results.append(result)

            logger.info(
                f"[진화] 변경 평가: {change.parameter} -> {result_str} "
                f"(승률 {change.win_rate_before:.1f}% -> {current_win_rate:.1f}%)"
            )

            # 평가 완료된 항목 제거
            if change in self.state.active_changes:
                self.state.active_changes.remove(change)

        self._save_state()
        return results

    async def _rollback_change(self, change: ParameterChange):
        """변경 롤백"""
        try:
            param_key = f"{change.strategy}.{change.parameter}"

            # 원래 값으로 복원
            if param_key in self._param_setters:
                self._param_setters[param_key](change.old_value)
            elif change.strategy in self._strategies:
                strategy = self._strategies[change.strategy]
                if hasattr(strategy, 'config') and hasattr(strategy.config, change.parameter):
                    setattr(strategy.config, change.parameter, change.old_value)

            self.state.rolled_back_changes += 1

            # 롤백 기록
            rollback_record = ParameterChange(
                timestamp=datetime.now(),
                strategy=change.strategy,
                parameter=change.parameter,
                old_value=change.new_value,
                new_value=change.old_value,
                reason=f"자동 롤백 (승률 {change.win_rate_after:.1f}% < {change.win_rate_before:.1f}%)",
                source="rollback",
            )
            self.state.change_history.append(rollback_record)

            logger.warning(
                f"[진화] 롤백: {param_key} = {change.new_value} -> {change.old_value}"
            )

        except Exception as e:
            logger.error(f"[진화] 롤백 실패: {change.parameter} - {e}")

    def _log_advice(self, advice: StrategyAdvice):
        """조언 로깅"""
        # 조언 파일 저장
        advice_file = self.storage_dir / f"advice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        with open(advice_file, "w", encoding="utf-8") as f:
            json.dump(advice.to_dict(), f, ensure_ascii=False, indent=2)

        # 핵심 인사이트 로깅
        logger.info(f"[진화] 전체 평가: {advice.overall_assessment}")
        for insight in advice.key_insights[:5]:
            logger.info(f"  - {insight}")

        if advice.avoid_situations:
            logger.info("[진화] 피해야 할 상황:")
            for situation in advice.avoid_situations[:3]:
                logger.info(f"  - {situation}")

    def get_evolution_summary(self) -> Dict:
        """진화 요약"""
        return {
            "version": self.state.version,
            "total_evolutions": self.state.total_evolutions,
            "last_evolution": self.state.last_evolution.isoformat() if self.state.last_evolution else None,
            "active_changes": len(self.state.active_changes),
            "successful_changes": self.state.successful_changes,
            "rolled_back_changes": self.state.rolled_back_changes,
            "success_rate": (
                self.state.successful_changes /
                (self.state.successful_changes + self.state.rolled_back_changes) * 100
                if (self.state.successful_changes + self.state.rolled_back_changes) > 0
                else 0
            ),
        }

    def get_evolution_state(self) -> Optional[EvolutionState]:
        """현재 진화 상태 반환"""
        return self.state

    async def rollback_last_change(self) -> bool:
        """마지막 변경 롤백"""
        if not self.state.active_changes:
            logger.warning("[진화] 롤백할 활성 변경 사항 없음")
            return False

        # 가장 최근 변경 롤백
        last_change = self.state.active_changes[-1]
        await self._rollback_change(last_change)

        # 활성 변경에서 제거
        self.state.active_changes.remove(last_change)
        self._save_state()

        return True

    async def manual_adjust(
        self,
        strategy: str,
        parameter: str,
        new_value: Any,
        reason: str = "수동 조정",
    ) -> bool:
        """수동 파라미터 조정"""
        param_key = f"{strategy}.{parameter}"

        # 현재 값 가져오기
        old_value = None
        if strategy in self._strategies:
            strat = self._strategies[strategy]
            if hasattr(strat, 'config') and hasattr(strat.config, parameter):
                old_value = getattr(strat.config, parameter)

        # 현재 성과
        current_review = self.reviewer.review_period(7)

        # 적용
        success = self._apply_parameter_change(
            param_key,
            old_value,
            new_value,
            reason,
            current_review,
        )

        if success:
            # 소스를 manual로 변경
            if self.state.active_changes:
                self.state.active_changes[-1].source = "manual"

            self._save_state()

        return success


# 싱글톤 인스턴스
_strategy_evolver: Optional[StrategyEvolver] = None


def get_strategy_evolver() -> StrategyEvolver:
    """StrategyEvolver 인스턴스 반환"""
    global _strategy_evolver
    if _strategy_evolver is None:
        _strategy_evolver = StrategyEvolver()
    return _strategy_evolver
