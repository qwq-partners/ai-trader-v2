"""
AI Trading Bot v2 - 일일 리포트 생성기

GPT-5.2 Pro를 활용하여 일일 거래 분석 및 복기를 수행합니다.

기능:
1. 일일 거래 요약
2. 승/패 분석
3. 전략별 성과 분석
4. AI 기반 거래 복기 (GPT-5.2 Pro)
5. 개선점 제안
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from typing import Dict, List, Optional, Any
from loguru import logger

from src.core.types import Position, Order, OrderSide
from src.utils.llm import LLMManager, LLMTask, get_llm_manager


@dataclass
class TradeRecord:
    """거래 기록"""
    symbol: str
    side: str
    quantity: int
    price: float
    timestamp: datetime
    strategy: str
    pnl: float = 0.0
    pnl_pct: float = 0.0
    reason: str = ""


@dataclass
class DailyStats:
    """일일 통계"""
    date: date
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    max_win: float = 0.0
    max_loss: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    trades: List[TradeRecord] = field(default_factory=list)
    strategy_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class DailyReporter:
    """
    일일 리포트 생성기

    GPT-5.2 Pro를 사용하여 깊은 분석과 거래 복기를 수행합니다.
    """

    def __init__(self, llm_manager: Optional[LLMManager] = None):
        self.llm = llm_manager or get_llm_manager()
        self._trades: List[TradeRecord] = []
        self._initial_capital: float = 0.0

    def set_initial_capital(self, capital: float):
        """초기 자본 설정"""
        self._initial_capital = capital

    def record_trade(self, trade: TradeRecord):
        """거래 기록 추가"""
        self._trades.append(trade)
        logger.debug(f"거래 기록: {trade.symbol} {trade.side} @ {trade.price}")

    def record_fill(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        price: float,
        strategy: str,
        reason: str = ""
    ):
        """체결 기록 추가"""
        trade = TradeRecord(
            symbol=symbol,
            side=side.value,
            quantity=quantity,
            price=price,
            timestamp=datetime.now(),
            strategy=strategy,
            reason=reason,
        )
        self.record_trade(trade)

    def calculate_daily_stats(self, target_date: Optional[date] = None) -> DailyStats:
        """일일 통계 계산"""
        target_date = target_date or date.today()

        # 해당 날짜 거래 필터
        day_trades = [t for t in self._trades if t.timestamp.date() == target_date]

        stats = DailyStats(date=target_date, trades=day_trades)

        if not day_trades:
            return stats

        # 기본 통계
        stats.total_trades = len(day_trades)

        # 매도 거래에서 손익 계산
        sells = [t for t in day_trades if t.side == "sell"]
        buys = [t for t in day_trades if t.side == "buy"]

        # 손익 집계
        profits = [t.pnl for t in sells if t.pnl > 0]
        losses = [t.pnl for t in sells if t.pnl < 0]

        stats.wins = len(profits)
        stats.losses = len(losses)
        stats.win_rate = stats.wins / len(sells) * 100 if sells else 0

        stats.total_pnl = sum(t.pnl for t in sells)
        if self._initial_capital > 0:
            stats.total_pnl_pct = stats.total_pnl / self._initial_capital * 100

        if profits:
            stats.max_win = max(profits)
            stats.avg_win = sum(profits) / len(profits)

        if losses:
            stats.max_loss = min(losses)  # 가장 큰 손실 (음수)
            stats.avg_loss = sum(losses) / len(losses)

        # 손익비
        total_profit = sum(profits) if profits else 0
        total_loss = abs(sum(losses)) if losses else 0
        stats.profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')

        # 전략별 통계
        strategy_trades: Dict[str, List[TradeRecord]] = {}
        for trade in sells:
            if trade.strategy not in strategy_trades:
                strategy_trades[trade.strategy] = []
            strategy_trades[trade.strategy].append(trade)

        for strategy, trades in strategy_trades.items():
            wins = sum(1 for t in trades if t.pnl > 0)
            losses = sum(1 for t in trades if t.pnl < 0)
            total_pnl = sum(t.pnl for t in trades)

            stats.strategy_stats[strategy] = {
                "trades": len(trades),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / len(trades) * 100 if trades else 0,
                "total_pnl": total_pnl,
            }

        return stats

    async def generate_report(
        self,
        stats: Optional[DailyStats] = None,
        include_ai_analysis: bool = True
    ) -> str:
        """일일 리포트 생성"""
        stats = stats or self.calculate_daily_stats()

        # 기본 리포트
        report = self._generate_basic_report(stats)

        # AI 분석 추가
        if include_ai_analysis and stats.total_trades > 0:
            ai_analysis = await self._generate_ai_analysis(stats)
            report += "\n\n" + ai_analysis

        return report

    def _generate_basic_report(self, stats: DailyStats) -> str:
        """기본 리포트 생성"""
        lines = [
            "=" * 60,
            f"📊 일일 트레이딩 리포트 - {stats.date}",
            "=" * 60,
            "",
            "📈 성과 요약",
            "-" * 40,
            f"총 거래: {stats.total_trades}회",
            f"승/패: {stats.wins}/{stats.losses} (승률 {stats.win_rate:.1f}%)",
            f"총 손익: {stats.total_pnl:+,.0f}원 ({stats.total_pnl_pct:+.2f}%)",
            f"최대 수익: {stats.max_win:+,.0f}원",
            f"최대 손실: {stats.max_loss:+,.0f}원",
            f"평균 수익: {stats.avg_win:+,.0f}원",
            f"평균 손실: {stats.avg_loss:+,.0f}원",
            f"손익비 (Profit Factor): {stats.profit_factor:.2f}",
            "",
        ]

        # 전략별 성과
        if stats.strategy_stats:
            lines.extend([
                "📋 전략별 성과",
                "-" * 40,
            ])
            for strategy, data in stats.strategy_stats.items():
                lines.append(
                    f"  {strategy}: {data['trades']}거래, "
                    f"승률 {data['win_rate']:.0f}%, "
                    f"손익 {data['total_pnl']:+,.0f}원"
                )
            lines.append("")

        # 개별 거래 내역
        if stats.trades:
            lines.extend([
                "📝 거래 내역",
                "-" * 40,
            ])
            for trade in stats.trades[-10:]:  # 최근 10건
                lines.append(
                    f"  {trade.timestamp.strftime('%H:%M')} | {trade.symbol} | "
                    f"{trade.side.upper()} {trade.quantity}주 @ {trade.price:,.0f}원 | "
                    f"손익: {trade.pnl:+,.0f}원 | {trade.strategy}"
                )

        return "\n".join(lines)

    async def _generate_ai_analysis(self, stats: DailyStats) -> str:
        """AI 기반 거래 분석 (GPT-5.2 Pro 사용)"""
        try:
            # 분석용 데이터 준비
            trades_data = []
            for trade in stats.trades:
                trades_data.append({
                    "time": trade.timestamp.strftime("%H:%M"),
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "price": trade.price,
                    "quantity": trade.quantity,
                    "pnl": trade.pnl,
                    "pnl_pct": trade.pnl_pct,
                    "strategy": trade.strategy,
                    "reason": trade.reason,
                })

            summary = {
                "date": str(stats.date),
                "total_trades": stats.total_trades,
                "wins": stats.wins,
                "losses": stats.losses,
                "win_rate": stats.win_rate,
                "total_pnl": stats.total_pnl,
                "total_pnl_pct": stats.total_pnl_pct,
                "profit_factor": stats.profit_factor,
                "strategy_stats": stats.strategy_stats,
                "trades": trades_data,
            }

            prompt = f"""당신은 전문 트레이딩 코치입니다.

오늘의 트레이딩 결과를 분석하고 건설적인 피드백을 제공해주세요.

## 오늘의 거래 데이터
```json
{json.dumps(summary, ensure_ascii=False, indent=2)}
```

다음 내용을 포함해서 분석해주세요:

1. **오늘의 핵심 평가** (한 문장)
2. **잘한 점** (2-3가지)
3. **개선할 점** (2-3가지)
4. **구체적인 개선 제안** (다음 거래에 적용할 수 있는 액션)
5. **전략별 피드백** (각 전략의 효과성 평가)

한국어로 간결하고 실용적으로 작성해주세요. 총 500자 이내로 작성해주세요."""

            # GPT-5.2 Pro로 깊은 분석 (HEAVY 태스크)
            analysis = await self.llm.generate(
                prompt=prompt,
                task=LLMTask.HEAVY,  # 깊은 분석용
                max_tokens=800,
            )

            if analysis:
                lines = [
                    "",
                    "🤖 AI 거래 복기 (GPT-5.2 Pro)",
                    "-" * 40,
                    analysis,
                ]
                return "\n".join(lines)
            else:
                return "\n🤖 AI 분석을 생성할 수 없습니다."

        except Exception as e:
            logger.error(f"AI 분석 생성 오류: {e}")
            return f"\n🤖 AI 분석 오류: {e}"

    def clear_trades(self):
        """거래 기록 초기화"""
        self._trades.clear()

    def get_trades(self, target_date: Optional[date] = None) -> List[TradeRecord]:
        """거래 기록 조회"""
        if target_date:
            return [t for t in self._trades if t.timestamp.date() == target_date]
        return self._trades.copy()


# 전역 인스턴스
_reporter: Optional[DailyReporter] = None


def get_daily_reporter() -> DailyReporter:
    """전역 리포터 인스턴스"""
    global _reporter
    if _reporter is None:
        _reporter = DailyReporter()
    return _reporter


async def generate_daily_report(
    initial_capital: float = 0,
    include_ai_analysis: bool = True
) -> str:
    """일일 리포트 생성 (편의 함수)"""
    reporter = get_daily_reporter()
    if initial_capital > 0:
        reporter.set_initial_capital(initial_capital)
    return await reporter.generate_report(include_ai_analysis=include_ai_analysis)
