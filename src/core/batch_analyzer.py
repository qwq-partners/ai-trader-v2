"""
AI Trading Bot v2 - 배치 분석 엔진

스윙 모멘텀 전체 배치 분석/실행/모니터링의 중심 모듈.

흐름:
  [15:40] run_daily_scan()
    → SwingScreener.run_full_scan() → 기술적 지표 → 전략별 시그널 → JSON 저장

  [09:01] execute_pending_signals()
    → JSON 로드 → 현재가 확인 → 진입 범위 내면 주문

  [매 30분] monitor_positions()
    → REST API 현재가 → ExitManager 체크 → 청산 주문
"""

import asyncio
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .event import SignalEvent
from .types import (
    Signal, OrderSide, SignalStrength, StrategyType
)


@dataclass
class PendingSignal:
    """대기 시그널 (JSON 직렬화 가능)"""
    symbol: str
    name: str
    strategy: str  # "rsi2_reversal" | "sepa_trend"
    side: str  # "buy"
    entry_price: float
    max_entry_price: float  # entry_price × 1.03
    stop_price: float
    target_price: float
    score: float
    reason: str
    created_at: str  # ISO format
    expires_at: str  # ISO format
    atr_pct: float = 0.0  # ATR % (ExitManager 전달용)

    def is_expired(self) -> bool:
        return datetime.now() > datetime.fromisoformat(self.expires_at)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PendingSignal":
        # 이전 JSON 호환: atr_pct 없으면 기본값 0.0
        data = d.copy()
        data.setdefault("atr_pct", 0.0)
        return cls(**data)


class BatchAnalyzer:
    """스윙 모멘텀 배치 분석 엔진"""

    def __init__(self, engine, broker, kis_market_data, stock_master=None,
                 exit_manager=None, config: Optional[Dict] = None):
        from ..signals.screener.swing_screener import SwingScreener
        from ..strategies.rsi2_reversal import RSI2ReversalStrategy
        from ..strategies.sepa_trend import SEPATrendStrategy
        from ..strategies.base import StrategyConfig

        self._engine = engine
        self._broker = broker
        self._kis_market_data = kis_market_data
        self._exit_manager = exit_manager
        self._config = config or {}

        # 스크리너
        self._screener = SwingScreener(broker, kis_market_data, stock_master)

        # 전략 인스턴스
        rsi2_cfg = StrategyConfig(
            name="RSI2Reversal",
            strategy_type=StrategyType.RSI2_REVERSAL,
            min_score=self._config.get("rsi2_reversal", {}).get("min_score", 65.0),
            params=self._config.get("rsi2_reversal", {}),
        )
        sepa_cfg = StrategyConfig(
            name="SEPATrend",
            strategy_type=StrategyType.SEPA_TREND,
            min_score=self._config.get("sepa_trend", {}).get("min_score", 70.0),
            params=self._config.get("sepa_trend", {}),
        )
        self._rsi2 = RSI2ReversalStrategy(rsi2_cfg)
        self._sepa = SEPATrendStrategy(sepa_cfg)

        # strategic_swing 최소 점수 (2계층 이상 복합 시그널만)
        self._strategic_min_score = self._config.get(
            "strategic_swing", {}
        ).get("min_score", 70.0)

        # 대기 시그널
        self._pending: List[PendingSignal] = []
        self._signals_path = Path.home() / ".cache" / "ai_trader" / "pending_signals.json"
        self._signals_path.parent.mkdir(parents=True, exist_ok=True)

        # 설정
        self._max_entry_slippage_pct = self._config.get("batch", {}).get(
            "max_entry_slippage_pct", 3.0
        )
        self._max_holding_days = self._config.get("batch", {}).get(
            "max_holding_days", 10
        )

    @staticmethod
    def _safe_strategy_type(strategy_str: Optional[str]) -> StrategyType:
        """문자열을 StrategyType으로 안전하게 변환 (ValueError 방지)"""
        if not strategy_str:
            return StrategyType.MOMENTUM_BREAKOUT
        try:
            return StrategyType(strategy_str)
        except (ValueError, KeyError):
            return StrategyType.MOMENTUM_BREAKOUT

    async def run_daily_scan(self):
        """[15:40] 일일 배치 스캔"""
        logger.info("[배치분석] ===== 일일 스캔 시작 =====")

        try:
            # 스크리너 실행
            candidates = await self._screener.run_full_scan()

            if not candidates:
                logger.info("[배치분석] 후보 종목 없음")
                self._pending = []
                self._save_json()
                return

            # 전략별 시그널 생성
            rsi2_candidates = [c for c in candidates if c.strategy == "rsi2_reversal"]
            sepa_candidates = [c for c in candidates if c.strategy == "sepa_trend"]

            rsi2_signals = await self._rsi2.generate_batch_signals(rsi2_candidates)
            sepa_signals = await self._sepa.generate_batch_signals(sepa_candidates)

            # strategic_swing 시그널: 2계층+ 복합신호 종목
            strategic_signals = self._generate_strategic_signals(candidates)

            all_signals = rsi2_signals + sepa_signals + strategic_signals

            # PendingSignal 변환
            self._pending = []
            now = datetime.now()
            # 익영업일 15:30 만료 (대략 익일)
            expires = now + timedelta(days=1)
            expires = expires.replace(hour=15, minute=30, second=0, microsecond=0)

            for sig in all_signals:
                entry_price = float(sig.price) if sig.price else 0
                if entry_price <= 0:
                    continue

                max_entry = entry_price * (1 + self._max_entry_slippage_pct / 100)

                pending = PendingSignal(
                    symbol=sig.symbol,
                    name=sig.metadata.get("candidate_name", sig.symbol),
                    strategy=sig.strategy.value,
                    side=sig.side.value,
                    entry_price=entry_price,
                    max_entry_price=max_entry,
                    stop_price=float(sig.stop_price) if sig.stop_price else entry_price * 0.95,
                    target_price=float(sig.target_price) if sig.target_price else entry_price * 1.10,
                    score=sig.score,
                    reason=sig.reason,
                    created_at=now.isoformat(),
                    expires_at=expires.isoformat(),
                    atr_pct=float(sig.metadata.get("atr_pct", 0)),
                )
                self._pending.append(pending)

            # JSON 저장
            self._save_json()

            logger.info(
                f"[배치분석] 스캔 완료: "
                f"RSI2={len(rsi2_signals)}개, SEPA={len(sepa_signals)}개, "
                f"전략스윙={len(strategic_signals)}개 → "
                f"대기 시그널 {len(self._pending)}개 저장"
            )

            # 텔레그램 알림
            await self._send_telegram_report()

        except Exception as e:
            logger.error(f"[배치분석] 일일 스캔 오류: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def execute_pending_signals(self):
        """[09:01] 대기 시그널 실행"""
        logger.info("[배치분석] ===== 대기 시그널 실행 =====")

        signals = self._load_json()
        if not signals:
            logger.info("[배치분석] 대기 시그널 없음")
            return

        executed = 0
        skipped = 0

        for sig in signals:
            try:
                if sig.is_expired():
                    logger.debug(f"[배치분석] {sig.symbol} 만료됨")
                    skipped += 1
                    continue

                # 현재가 조회
                quote = await self._broker.get_quote(sig.symbol)
                if not quote:
                    logger.warning(f"[배치분석] {sig.symbol} 현재가 조회 실패")
                    skipped += 1
                    continue

                current_price = float(quote.get("price", 0))
                if current_price <= 0:
                    skipped += 1
                    continue

                # 진입 범위 체크
                if current_price > sig.max_entry_price:
                    logger.info(
                        f"[배치분석] {sig.symbol} 진입 스킵: "
                        f"현재가 {current_price:,.0f} > 최대진입가 {sig.max_entry_price:,.0f}"
                    )
                    skipped += 1
                    continue

                # 이미 보유 중인 종목 스킵
                if sig.symbol in self._engine.portfolio.positions:
                    logger.info(f"[배치분석] {sig.symbol} 이미 보유 중, 스킵")
                    skipped += 1
                    continue

                # 기존 이벤트 시스템으로 Signal 발행
                try:
                    strategy_type = StrategyType(sig.strategy)
                except (ValueError, KeyError):
                    strategy_type = StrategyType.MOMENTUM_BREAKOUT
                signal = Signal(
                    symbol=sig.symbol,
                    side=OrderSide.BUY,
                    strength=SignalStrength.STRONG,
                    strategy=strategy_type,
                    price=Decimal(str(current_price)),
                    target_price=Decimal(str(sig.target_price)),
                    stop_price=Decimal(str(sig.stop_price)),
                    score=sig.score,
                    confidence=sig.score / 100.0,
                    reason=sig.reason,
                    metadata={
                        "batch_signal": True,
                        "name": sig.name,
                        "atr_pct": sig.atr_pct,
                    },
                )

                # 종목명 캐시에 저장 (매수 시그널/주문 이벤트에 종목명 표시)
                name_cache = getattr(self._engine, '_stock_name_cache', None)
                if name_cache is not None and sig.name and sig.name != sig.symbol:
                    name_cache[sig.symbol] = sig.name

                event = SignalEvent.from_signal(signal, source="batch_analyzer")
                await self._engine.emit(event)
                executed += 1

                logger.info(
                    f"[배치분석] {sig.symbol} {sig.name} 시그널 발행: "
                    f"현재가={current_price:,.0f} 전략={sig.strategy} 점수={sig.score:.0f}"
                )

                # Rate limit
                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"[배치분석] {sig.symbol} 실행 오류: {e}")
                skipped += 1

        logger.info(f"[배치분석] 실행 완료: 발행={executed}개, 스킵={skipped}개")

        # 실행 완료 후 시그널 파일 비우기 (재시작 시 중복 방지)
        self._pending = []
        self._save_json()

    async def monitor_positions(self):
        """[매 30분] 보유 포지션 시세 갱신 + 청산 체크"""
        if not self._engine.portfolio.positions:
            return

        logger.debug(f"[포지션모니터] {len(self._engine.portfolio.positions)}개 포지션 체크")

        for symbol, pos in list(self._engine.portfolio.positions.items()):
            try:
                # REST API 현재가 조회
                quote = await self._broker.get_quote(symbol)
                if not quote:
                    continue

                current_price = Decimal(str(quote.get("price", 0)))
                if current_price <= 0:
                    continue

                # 포지션 가격 갱신
                pos.current_price = current_price
                if pos.highest_price is None or current_price > pos.highest_price:
                    pos.highest_price = current_price

                # ExitManager 청산 체크
                if self._exit_manager:
                    exit_result = self._exit_manager.update_price(symbol, current_price)
                    if exit_result:
                        action, qty, reason = exit_result
                        logger.info(f"[포지션모니터] {symbol} 청산 시그널: {reason} ({qty}주)")

                        # 매도 시그널 → 이벤트 시스템
                        signal = Signal(
                            symbol=symbol,
                            side=OrderSide.SELL,
                            strength=SignalStrength.STRONG,
                            strategy=self._safe_strategy_type(pos.strategy),
                            price=current_price,
                            score=100,
                            confidence=1.0,
                            reason=reason,
                        )
                        event = SignalEvent.from_signal(signal, source="position_monitor")
                        await self._engine.emit(event)
                        await asyncio.sleep(0.2)  # rate limit
                        continue  # 청산 시그널 발행 시 보유기간 체크 스킵

                # 보유기간 초과 강제 청산
                if pos.entry_time:
                    holding_days = (datetime.now() - pos.entry_time).days
                    if holding_days > self._max_holding_days:
                        logger.info(
                            f"[포지션모니터] {symbol} 보유기간 초과: {holding_days}일 "
                            f"(최대 {self._max_holding_days}일)"
                        )
                        signal = Signal(
                            symbol=symbol,
                            side=OrderSide.SELL,
                            strength=SignalStrength.NORMAL,
                            strategy=self._safe_strategy_type(pos.strategy),
                            price=current_price,
                            score=80,
                            confidence=0.8,
                            reason=f"보유기간 초과: {holding_days}일>{self._max_holding_days}일",
                        )
                        event = SignalEvent.from_signal(signal, source="position_monitor")
                        await self._engine.emit(event)

                await asyncio.sleep(0.2)  # rate limit

            except Exception as e:
                logger.warning(f"[포지션모니터] {symbol} 체크 오류: {e}")

    def _generate_strategic_signals(self, candidates) -> List[Signal]:
        """strategic_swing 시그널 생성: 2계층 이상 복합신호 종목"""
        signals = []
        for c in candidates:
            # 2계층 이상 복합신호 확인 (구조화된 메타데이터 기반)
            layers = c.indicators.get("strategic_layers", 0)
            if layers < 2:
                continue
            if c.score < self._strategic_min_score:
                continue

            entry_price = float(c.entry_price) if c.entry_price else 0
            if entry_price <= 0:
                continue

            signal = Signal(
                symbol=c.symbol,
                side=OrderSide.BUY,
                strength=SignalStrength.STRONG,
                strategy=StrategyType.STRATEGIC_SWING,
                price=c.entry_price,
                target_price=c.target_price,
                stop_price=c.stop_price,
                score=c.score,
                confidence=min(c.score / 100.0, 1.0),
                reason=f"전략적 스윙: {', '.join(c.reasons[:3])}",
                metadata={
                    "candidate_name": c.name,
                    "atr_pct": c.indicators.get("atr_pct", 0),
                    "strategic_layers": sum(
                        1 for r in c.reasons
                        if any(kw in r for kw in ["전문가패널", "수급추세", "VCP"])
                    ),
                },
            )
            signals.append(signal)

        logger.info(f"[배치분석] 전략스윙 시그널 {len(signals)}개 생성")
        return signals

    def _save_json(self):
        """대기 시그널 JSON 저장"""
        try:
            data = [p.to_dict() for p in self._pending]
            with open(self._signals_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"[배치분석] {len(self._pending)}개 시그널 저장: {self._signals_path}")
        except Exception as e:
            logger.error(f"[배치분석] JSON 저장 실패: {e}")

    def _load_json(self) -> List[PendingSignal]:
        """대기 시그널 JSON 로드"""
        try:
            if not self._signals_path.exists():
                return []
            with open(self._signals_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [PendingSignal.from_dict(d) for d in data]
        except Exception as e:
            logger.error(f"[배치분석] JSON 로드 실패: {e}")
            return []

    async def _send_telegram_report(self):
        """스캔 결과 텔레그램 알림"""
        try:
            from ..utils.telegram import send_alert

            if not self._pending:
                await send_alert(
                    "🔍 <b>일일 스윙 스캔 완료</b>\n\n"
                    "후보 종목: <b>0개</b>"
                )
                return

            # 전략별 분류
            strat_names = {
                "sepa_trend": "SEPA",
                "rsi2_reversal": "RSI2",
                "strategic_swing": "전략스윙",
                "momentum_breakout": "모멘텀",
            }
            strat_counts = {}
            for p in self._pending:
                sn = strat_names.get(p.strategy, p.strategy)
                strat_counts[sn] = strat_counts.get(sn, 0) + 1

            strat_summary = " / ".join(f"{k} {v}개" for k, v in strat_counts.items())

            lines = [
                f"🔍 <b>일일 스윙 스캔 완료</b>",
                f"",
                f"후보: <b>{len(self._pending)}개</b> ({strat_summary})",
                f"",
            ]

            strat_emoji = {
                "sepa_trend": "🟢",
                "rsi2_reversal": "🔵",
                "strategic_swing": "🟣",
                "momentum_breakout": "🟠",
            }

            for i, p in enumerate(self._pending[:10], 1):
                emoji = strat_emoji.get(p.strategy, "⚪")
                sn = strat_names.get(p.strategy, p.strategy)
                pnl_target = (p.target_price / p.entry_price - 1) * 100 if p.entry_price > 0 else 0
                pnl_stop = (p.stop_price / p.entry_price - 1) * 100 if p.entry_price > 0 else 0
                lines.append(
                    f"{emoji} <b>{p.name}</b> <code>{p.symbol}</code> "
                    f"| {sn} {p.score:.0f}점"
                )
                lines.append(
                    f"    진입 {p.entry_price:,.0f} → "
                    f"목표 {p.target_price:,.0f}(<b>+{pnl_target:.1f}%</b>) / "
                    f"손절 {p.stop_price:,.0f}({pnl_stop:.1f}%)"
                )
                if p.reason:
                    # reason이 너무 길면 축약
                    reason_display = p.reason if len(p.reason) <= 60 else p.reason[:57] + "..."
                    lines.append(f"    💡 {reason_display}")
                lines.append("")

            if len(self._pending) > 10:
                lines.append(f"<i>... 외 {len(self._pending) - 10}개 종목</i>")

            await send_alert("\n".join(lines))

        except Exception as e:
            logger.warning(f"[배치분석] 텔레그램 알림 실패: {e}")
