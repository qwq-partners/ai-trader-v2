"""
AI Trading Bot v2 - 거래 복기 시스템 (Trade Reviewer)

거래 결과를 자동으로 분석하고 패턴을 찾습니다.
"""

from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any, Tuple
from loguru import logger

from .trade_journal import TradeJournal, TradeRecord, get_trade_journal


@dataclass
class ReviewResult:
    """복기 결과"""
    # 기간
    period_start: datetime
    period_end: datetime
    total_trades: int

    # 성과 요약
    win_rate: float                      # 승률 (%)
    avg_pnl_pct: float                   # 평균 수익률 (%)
    total_pnl: float                     # 총 손익 (원)
    profit_factor: float                 # 손익비 (총이익/총손실)
    max_drawdown_pct: float              # 최대 낙폭 (%)

    # 패턴 분석
    winning_patterns: List[Dict] = field(default_factory=list)   # 성공 패턴
    losing_patterns: List[Dict] = field(default_factory=list)    # 실패 패턴
    strategy_performance: Dict[str, Dict] = field(default_factory=dict)  # 전략별 성과

    # 시간대 분석
    best_entry_hours: List[int] = field(default_factory=list)    # 최적 진입 시간
    worst_entry_hours: List[int] = field(default_factory=list)   # 최악 진입 시간

    # 지표 분석
    optimal_indicators: Dict[str, Dict] = field(default_factory=dict)    # 최적 지표 범위
    avoid_indicators: Dict[str, Dict] = field(default_factory=dict)      # 피해야 할 지표 범위

    # 개선 포인트
    issues: List[str] = field(default_factory=list)              # 발견된 문제점
    suggestions: List[str] = field(default_factory=list)         # 개선 제안

    # LLM 분석용 요약
    summary_for_llm: str = ""

    def to_dict(self) -> Dict:
        """딕셔너리로 변환"""
        return {
            "period": {
                "start": self.period_start.isoformat(),
                "end": self.period_end.isoformat(),
            },
            "performance": {
                "total_trades": self.total_trades,
                "win_rate": self.win_rate,
                "avg_pnl_pct": self.avg_pnl_pct,
                "total_pnl": self.total_pnl,
                "profit_factor": self.profit_factor,
                "max_drawdown_pct": self.max_drawdown_pct,
            },
            "patterns": {
                "winning": self.winning_patterns,
                "losing": self.losing_patterns,
            },
            "strategy_performance": self.strategy_performance,
            "timing": {
                "best_hours": self.best_entry_hours,
                "worst_hours": self.worst_entry_hours,
            },
            "indicators": {
                "optimal": self.optimal_indicators,
                "avoid": self.avoid_indicators,
            },
            "issues": self.issues,
            "suggestions": self.suggestions,
            "summary": self.summary_for_llm,
        }


class TradeReviewer:
    """
    거래 복기 시스템

    거래 기록을 분석하여:
    1. 성공/실패 패턴 추출
    2. 최적 진입 조건 도출
    3. 피해야 할 상황 식별
    4. 전략별 성과 비교
    """

    def __init__(self, journal: TradeJournal = None):
        self.journal = journal or get_trade_journal()

    def review_period(
        self,
        days: int = 7,
        strategy: str = None,
    ) -> ReviewResult:
        """
        기간 복기

        최근 N일간의 거래를 분석합니다.
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        # 거래 조회
        if strategy:
            trades = self.journal.get_trades_by_strategy(strategy, days)
        else:
            trades = self.journal.get_closed_trades(days)

        if not trades:
            return ReviewResult(
                period_start=start_date,
                period_end=end_date,
                total_trades=0,
                win_rate=0,
                avg_pnl_pct=0,
                total_pnl=0,
                profit_factor=0,
                max_drawdown_pct=0,
            )

        # 기본 통계 계산
        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]

        total_profit = sum(t.pnl for t in wins) if wins else 0
        total_loss = abs(sum(t.pnl for t in losses)) if losses else 0

        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_pnl_pct = sum(t.pnl_pct for t in trades) / len(trades) if trades else 0
        total_pnl = sum(t.pnl for t in trades)
        # 손실 0원 시 profit_factor 상한 99.9 (LLM 왜곡 방지)
        profit_factor = min(total_profit / total_loss, 99.9) if total_loss > 0 else (99.9 if total_profit > 0 else 0)

        # 최대 낙폭 계산
        max_drawdown = self._calculate_max_drawdown(trades)

        # 패턴 분석
        winning_patterns = self._analyze_winning_patterns(wins)
        losing_patterns = self._analyze_losing_patterns(losses)

        # 전략별 성과
        strategy_perf = self._analyze_strategy_performance(trades)

        # 시간대 분석
        best_hours, worst_hours = self._analyze_entry_timing(trades)

        # 지표 분석
        optimal_indicators, avoid_indicators = self._analyze_indicators(wins, losses)

        # 문제점 및 개선 제안
        issues = self._identify_issues(trades, win_rate, profit_factor)
        suggestions = self._generate_suggestions(
            trades, winning_patterns, losing_patterns, strategy_perf
        )

        # LLM용 요약 생성
        summary = self._generate_summary_for_llm(
            trades, win_rate, avg_pnl_pct, profit_factor,
            winning_patterns, losing_patterns, issues
        )

        return ReviewResult(
            period_start=start_date,
            period_end=end_date,
            total_trades=len(trades),
            win_rate=win_rate,
            avg_pnl_pct=avg_pnl_pct,
            total_pnl=total_pnl,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown,
            winning_patterns=winning_patterns,
            losing_patterns=losing_patterns,
            strategy_performance=strategy_perf,
            best_entry_hours=best_hours,
            worst_entry_hours=worst_hours,
            optimal_indicators=optimal_indicators,
            avoid_indicators=avoid_indicators,
            issues=issues,
            suggestions=suggestions,
            summary_for_llm=summary,
        )

    def _calculate_max_drawdown(self, trades: List[TradeRecord]) -> float:
        """최대 낙폭 계산"""
        if not trades:
            return 0

        # 시간순 정렬
        sorted_trades = sorted(trades, key=lambda t: t.entry_time or datetime.min)

        cumulative = 0
        peak = 0
        max_drawdown = 0

        for trade in sorted_trades:
            cumulative += trade.pnl_pct
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        return max_drawdown

    def _analyze_winning_patterns(self, wins: List[TradeRecord]) -> List[Dict]:
        """성공 패턴 분석"""
        patterns = []

        if not wins:
            return patterns

        # 1. 진입 시간대 패턴
        hour_wins = {}
        for trade in wins:
            if trade.entry_time:
                hour = trade.entry_time.hour
                hour_wins[hour] = hour_wins.get(hour, 0) + 1

        if hour_wins:
            best_hour = max(hour_wins, key=hour_wins.get)
            patterns.append({
                "type": "timing",
                "description": f"진입 시간 {best_hour}시에서 가장 높은 성공률",
                "detail": f"{hour_wins[best_hour]}회 성공",
            })

        # 2. 진입 지표 패턴
        indicator_ranges = self._get_indicator_ranges(wins)
        for indicator, (min_val, max_val, avg_val) in indicator_ranges.items():
            if indicator in ["change_1d", "vol_ratio", "rsi"]:
                patterns.append({
                    "type": "indicator",
                    "description": f"성공 시 {indicator} 평균 {avg_val:.1f}",
                    "range": {"min": min_val, "max": max_val, "avg": avg_val},
                })

        # 3. 보유 시간 패턴
        avg_holding = sum(t.holding_minutes for t in wins) / len(wins)
        patterns.append({
            "type": "holding",
            "description": f"성공 거래 평균 보유 시간: {avg_holding:.0f}분",
            "avg_minutes": avg_holding,
        })

        # 4. 전략 패턴
        strategy_wins = {}
        for trade in wins:
            strategy = trade.entry_strategy or "unknown"
            strategy_wins[strategy] = strategy_wins.get(strategy, 0) + 1

        if strategy_wins:
            best_strategy = max(strategy_wins, key=strategy_wins.get)
            patterns.append({
                "type": "strategy",
                "description": f"{best_strategy} 전략이 가장 많은 성공",
                "count": strategy_wins[best_strategy],
            })

        return patterns

    def _analyze_losing_patterns(self, losses: List[TradeRecord]) -> List[Dict]:
        """실패 패턴 분석"""
        patterns = []

        if not losses:
            return patterns

        # 1. 손절 유형 분석
        exit_types = {}
        for trade in losses:
            exit_type = trade.exit_type or "unknown"
            exit_types[exit_type] = exit_types.get(exit_type, 0) + 1

        for exit_type, count in exit_types.items():
            patterns.append({
                "type": "exit_type",
                "description": f"{exit_type}로 {count}회 손실",
                "count": count,
            })

        # 2. 과열 진입 패턴
        overheated = [t for t in losses
                      if t.indicators_at_entry.get("change_1d", 0) > 10]
        if overheated:
            patterns.append({
                "type": "overheated_entry",
                "description": f"10% 이상 급등 후 진입으로 {len(overheated)}회 손실",
                "count": len(overheated),
                "warning": "과열 종목 진입 자제 필요",
            })

        # 3. RSI 과매수 진입
        overbought = [t for t in losses
                      if t.indicators_at_entry.get("rsi", 50) > 70]
        if overbought:
            patterns.append({
                "type": "rsi_overbought",
                "description": f"RSI 70 이상에서 진입으로 {len(overbought)}회 손실",
                "count": len(overbought),
                "warning": "과매수 구간 진입 자제 필요",
            })

        # 4. 거래량 부족
        low_volume = [t for t in losses
                      if t.indicators_at_entry.get("vol_ratio", 1) < 1.5]
        if low_volume:
            patterns.append({
                "type": "low_volume",
                "description": f"거래량 부족 상태에서 {len(low_volume)}회 손실",
                "count": len(low_volume),
            })

        return patterns

    def _analyze_strategy_performance(self, trades: List[TradeRecord]) -> Dict[str, Dict]:
        """전략별 성과 분석"""
        stats = {}

        for trade in trades:
            strategy = trade.entry_strategy or "unknown"

            if strategy not in stats:
                stats[strategy] = {
                    "trades": 0,
                    "wins": 0,
                    "total_pnl": 0,
                    "total_pnl_pct": 0,
                    "avg_holding_minutes": 0,
                }

            stats[strategy]["trades"] += 1
            if trade.is_win:
                stats[strategy]["wins"] += 1
            stats[strategy]["total_pnl"] += trade.pnl
            stats[strategy]["total_pnl_pct"] += trade.pnl_pct
            stats[strategy]["avg_holding_minutes"] += trade.holding_minutes

        # 평균 계산
        for strategy, s in stats.items():
            count = s["trades"]
            if count > 0:
                s["win_rate"] = s["wins"] / count * 100
                s["avg_pnl_pct"] = s["total_pnl_pct"] / count
                s["avg_holding_minutes"] = s["avg_holding_minutes"] / count
            else:
                s["win_rate"] = 0
                s["avg_pnl_pct"] = 0

        return stats

    def _analyze_entry_timing(self, trades: List[TradeRecord]) -> Tuple[List[int], List[int]]:
        """진입 시간대 분석"""
        hour_stats = {}

        for trade in trades:
            if trade.entry_time:
                hour = trade.entry_time.hour
                if hour not in hour_stats:
                    hour_stats[hour] = {"wins": 0, "losses": 0}

                if trade.is_win:
                    hour_stats[hour]["wins"] += 1
                else:
                    hour_stats[hour]["losses"] += 1

        # 승률 계산
        hour_win_rates = {}
        for hour, stats in hour_stats.items():
            total = stats["wins"] + stats["losses"]
            if total >= 3:  # 최소 3회 이상
                hour_win_rates[hour] = stats["wins"] / total * 100

        if not hour_win_rates:
            return [], []

        # 정렬
        sorted_hours = sorted(hour_win_rates.items(), key=lambda x: x[1], reverse=True)

        best_hours = [h for h, rate in sorted_hours[:3] if rate >= 50]
        worst_hours = [h for h, rate in sorted_hours[-3:] if rate < 50]

        return best_hours, worst_hours

    def _analyze_indicators(
        self,
        wins: List[TradeRecord],
        losses: List[TradeRecord]
    ) -> Tuple[Dict, Dict]:
        """지표 분석"""
        optimal = {}
        avoid = {}

        # 승리 거래 지표 범위
        if wins:
            win_ranges = self._get_indicator_ranges(wins)
            for indicator, (min_val, max_val, avg_val) in win_ranges.items():
                optimal[indicator] = {
                    "min": min_val,
                    "max": max_val,
                    "avg": avg_val,
                    "sample_size": len(wins),
                }

        # 손실 거래 지표 범위 (피해야 할 구간)
        if losses:
            loss_ranges = self._get_indicator_ranges(losses)
            for indicator, (min_val, max_val, avg_val) in loss_ranges.items():
                # 승리 범위와 겹치지 않는 부분을 피해야 할 구간으로
                if indicator in optimal:
                    opt = optimal[indicator]
                    # 손실 평균이 승리 평균보다 높거나 낮으면 위험 구간
                    if avg_val > opt["avg"] * 1.2 or avg_val < opt["avg"] * 0.8:
                        avoid[indicator] = {
                            "risky_avg": avg_val,
                            "loss_rate_in_range": len(losses) / (len(wins) + len(losses)) * 100,
                        }

        return optimal, avoid

    def _get_indicator_ranges(self, trades: List[TradeRecord]) -> Dict[str, Tuple[float, float, float]]:
        """거래들의 지표 범위 계산"""
        indicators = {}

        for trade in trades:
            for key, value in trade.indicators_at_entry.items():
                if isinstance(value, (int, float)):
                    if key not in indicators:
                        indicators[key] = []
                    indicators[key].append(value)

        result = {}
        for key, values in indicators.items():
            if values:
                result[key] = (min(values), max(values), sum(values) / len(values))

        return result

    def _identify_issues(
        self,
        trades: List[TradeRecord],
        win_rate: float,
        profit_factor: float
    ) -> List[str]:
        """문제점 식별"""
        issues = []

        # 1. 낮은 승률
        if win_rate < 40:
            issues.append(f"승률이 {win_rate:.1f}%로 낮음 (40% 미만)")

        # 2. 낮은 손익비
        if profit_factor < 1.0:
            issues.append(f"손익비가 {profit_factor:.2f}로 1 미만 (손실이 이익보다 큼)")

        # 3. 연속 손실 패턴
        sorted_trades = sorted(trades, key=lambda t: t.entry_time or datetime.min)
        max_consecutive_losses = 0
        current_streak = 0

        for trade in sorted_trades:
            if not trade.is_win:
                current_streak += 1
                max_consecutive_losses = max(max_consecutive_losses, current_streak)
            else:
                current_streak = 0

        if max_consecutive_losses >= 3:
            issues.append(f"연속 {max_consecutive_losses}회 손실 발생")

        # 4. 과다 거래
        if len(trades) > 50:  # 주간 기준
            issues.append(f"과다 거래 ({len(trades)}회) - 선별적 진입 필요")

        # 5. 손절 미실행
        big_losses = [t for t in trades if t.pnl_pct < -5]
        if big_losses:
            issues.append(f"-5% 이상 큰 손실 {len(big_losses)}회 - 손절 규칙 준수 필요")

        return issues

    def _generate_suggestions(
        self,
        trades: List[TradeRecord],
        winning_patterns: List[Dict],
        losing_patterns: List[Dict],
        strategy_perf: Dict[str, Dict]
    ) -> List[str]:
        """개선 제안 생성"""
        suggestions = []

        # 1. 최고 성과 전략 강화
        if strategy_perf:
            best_strategy = max(
                strategy_perf.items(),
                key=lambda x: x[1].get("win_rate", 0)
            )
            if best_strategy[1].get("win_rate", 0) > 60:
                suggestions.append(
                    f"'{best_strategy[0]}' 전략 비중 확대 고려 "
                    f"(승률 {best_strategy[1]['win_rate']:.1f}%)"
                )

        # 2. 저성과 전략 축소
        if strategy_perf:
            worst_strategy = min(
                strategy_perf.items(),
                key=lambda x: x[1].get("win_rate", 100)
            )
            if worst_strategy[1].get("win_rate", 100) < 40 and worst_strategy[1].get("trades", 0) >= 5:
                suggestions.append(
                    f"'{worst_strategy[0]}' 전략 사용 재검토 "
                    f"(승률 {worst_strategy[1]['win_rate']:.1f}%)"
                )

        # 3. 패턴 기반 제안
        for pattern in losing_patterns:
            if pattern["type"] == "overheated_entry":
                suggestions.append("10% 이상 급등 종목 진입 자제 - 최대 등락률 제한 강화")
            if pattern["type"] == "rsi_overbought":
                suggestions.append("RSI 70 이상 구간 진입 필터 추가")
            if pattern["type"] == "low_volume":
                suggestions.append("거래량 비율 최소 기준 상향 조정 (1.5x → 2.0x)")

        # 4. 시간대 기반 제안
        if winning_patterns:
            for pattern in winning_patterns:
                if pattern["type"] == "timing":
                    suggestions.append(f"진입 시간대 집중: {pattern['description']}")

        return suggestions

    def _generate_summary_for_llm(
        self,
        trades: List[TradeRecord],
        win_rate: float,
        avg_pnl_pct: float,
        profit_factor: float,
        winning_patterns: List[Dict],
        losing_patterns: List[Dict],
        issues: List[str]
    ) -> str:
        """LLM 분석용 요약 텍스트 생성"""
        # 일평균 지표 계산 (공휴일 포함 실제 영업일)
        total_pnl = sum(t.pnl for t in trades)
        # entry_time이 None인 레코드 제외 (손상 데이터 방어)
        valid_trades = [t for t in trades if t.entry_time]
        if len(valid_trades) >= 2:
            sorted_trades = sorted(valid_trades, key=lambda t: t.entry_time)
            start_d = sorted_trades[0].entry_time.date()
            end_d = sorted_trades[-1].exit_time.date() if sorted_trades[-1].exit_time else date.today()
        else:
            start_d = end_d = date.today()
        period_days = max((end_d - start_d).days, 1)
        try:
            from ..engine import is_kr_market_holiday
            trading_days = 0
            d = start_d
            while d <= end_d:
                if d.weekday() < 5 and not is_kr_market_holiday(d):
                    trading_days += 1
                d += timedelta(days=1)
            trading_days = max(trading_days, 1)
        except ImportError:
            trading_days = max(period_days * 5 // 7, 1)
        daily_avg_pnl = total_pnl / trading_days
        daily_avg_pnl_pct = avg_pnl_pct * len(trades) / trading_days if trading_days > 0 else 0
        daily_trades = len(trades) / trading_days

        lines = [
            f"## 거래 복기 요약",
            f"",
            f"### 핵심 목표 달성 현황 (일평균 수익률 1% 목표)",
            f"- 일평균 수익률: {daily_avg_pnl_pct:+.2f}% (목표: +1.00%)",
            f"- 일평균 손익: {daily_avg_pnl:+,.0f}원",
            f"- 일평균 거래: {daily_trades:.1f}건",
            f"- 분석 기간: {period_days}일 (영업일 {trading_days}일)",
            f"",
            f"### 기본 성과",
            f"- 총 거래: {len(trades)}회",
            f"- 승률: {win_rate:.1f}%",
            f"- 평균 수익률 (건당): {avg_pnl_pct:+.2f}%",
            f"- 총 손익: {total_pnl:+,.0f}원",
            f"- 손익비: {profit_factor:.2f}",
            f"",
            f"### 성공 패턴",
        ]

        for pattern in winning_patterns[:5]:
            lines.append(f"- {pattern['description']}")

        lines.extend([
            f"",
            f"### 실패 패턴",
        ])

        for pattern in losing_patterns[:5]:
            lines.append(f"- {pattern['description']}")

        if issues:
            lines.extend([
                f"",
                f"### 발견된 문제점",
            ])
            for issue in issues:
                lines.append(f"- {issue}")

        lines.extend([
            f"",
            f"### 대표 거래 예시",
        ])

        # 최고/최저 거래
        if trades:
            best = max(trades, key=lambda t: t.pnl_pct)
            worst = min(trades, key=lambda t: t.pnl_pct)

            lines.append(f"**최고 거래**: {best.symbol} {best.name}")
            lines.append(f"  - 수익률: {best.pnl_pct:+.1f}%, 전략: {best.entry_strategy}")
            lines.append(f"  - 진입 사유: {best.entry_reason}")

            lines.append(f"**최악 거래**: {worst.symbol} {worst.name}")
            lines.append(f"  - 수익률: {worst.pnl_pct:+.1f}%, 전략: {worst.entry_strategy}")
            lines.append(f"  - 청산 사유: {worst.exit_reason}")

        return "\n".join(lines)


# 싱글톤 인스턴스
_trade_reviewer: Optional[TradeReviewer] = None


def get_trade_reviewer() -> TradeReviewer:
    """TradeReviewer 인스턴스 반환"""
    global _trade_reviewer
    if _trade_reviewer is None:
        _trade_reviewer = TradeReviewer()
    return _trade_reviewer
