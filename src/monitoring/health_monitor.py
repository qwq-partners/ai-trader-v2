"""
AI Trading Bot v2 - HealthMonitor

운영 중 이상 상태를 자동 감지하고 텔레그램/SSE로 알림.
단일 async 루프에서 3계층(critical/important/periodic) 체크를 시간 기반 분기.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from loguru import logger

from src.utils.telegram import send_alert


@dataclass
class CheckResult:
    name: str                       # 체크 이름 (예: "event_loop_stall")
    level: str                      # "critical" | "warning" | "info"
    ok: bool                        # True=정상, False=이상
    message: str                    # 상세 메시지
    value: Optional[float] = None   # 측정값 (대시보드 표시용)
    timestamp: datetime = field(default_factory=datetime.now)


class HealthMonitor:
    """운영 모니터링 — 이상 감지 + 알림"""

    _COOLDOWNS = {
        "critical": 300,    # 5분
        "warning": 900,     # 15분
        "info": 3600,       # 1시간
    }

    def __init__(self, bot):
        self.bot = bot
        self._alert_cooldowns: Dict[str, datetime] = {}
        self._last_events_processed: int = -1  # -1 = 미초기화 (첫 체크에서 baseline 스냅샷)
        self._last_events_check_time: datetime = datetime.now()
        self._results_map: Dict[str, CheckResult] = {}  # check_name → 최신 결과

    @property
    def _results(self) -> List[CheckResult]:
        """외부 접근용: 최신 체크 결과 리스트"""
        return list(self._results_map.values())

    # ==========================================================
    # 메인 루프
    # ==========================================================

    async def run_loop(self):
        """단일 async 루프 — 3개 계층을 시간 기반 분기"""
        tick = 0
        while self.bot.running:
            await asyncio.sleep(15)
            tick += 15

            results: List[CheckResult] = []

            # Critical 체크 (15초마다, 장중만)
            if self._is_market_hours():
                results += await self._check_critical()

            # Important 체크 (60초마다)
            if tick % 60 == 0:
                results += await self._check_important()

            # Periodic 체크 (5분마다)
            if tick % 300 == 0:
                results += await self._check_periodic()

            # 결과 저장 (check name 기반 누적 — 이전 틱의 결과 유지)
            for r in results:
                self._results_map[r.name] = r
            failed = [r for r in results if not r.ok]
            if failed:
                await self._handle_failures(failed)

            # 대시보드 SSE 브로드캐스트 (실패 항목만)
            if failed and self.bot.dashboard:
                try:
                    await self.bot.dashboard.sse_manager.broadcast(
                        "health_checks",
                        [{"name": r.name, "level": r.level, "message": r.message}
                         for r in failed],
                    )
                except Exception:
                    pass

    # ==========================================================
    # Critical 체크 (15초 주기, 장중만)
    # ==========================================================

    async def _check_critical(self) -> List[CheckResult]:
        results = []
        results.append(await self._check_event_loop_stall())
        results.append(await self._check_ws_feed())
        results.append(await self._check_daily_loss())
        results.append(await self._check_pending_deadlock())
        return results

    async def _check_event_loop_stall(self) -> CheckResult:
        """이벤트 루프 스톨: events_processed가 60초간 미증가"""
        engine = self.bot.engine
        current = engine.stats.events_processed
        now = datetime.now()

        # 첫 체크: baseline 스냅샷 (시작 직후 오탐 방지)
        if self._last_events_processed < 0:
            self._last_events_processed = current
            self._last_events_check_time = now
            return CheckResult("event_loop_stall", "critical", True, "정상 (초기화)")

        elapsed = (now - self._last_events_check_time).total_seconds()

        if elapsed >= 60 and current == self._last_events_processed:
            return CheckResult(
                "event_loop_stall", "critical", False,
                f"이벤트 루프 {elapsed:.0f}초 정지 (처리: {current}건)",
            )

        if current != self._last_events_processed:
            self._last_events_processed = current
            self._last_events_check_time = now

        return CheckResult("event_loop_stall", "critical", True, "정상")

    async def _check_ws_feed(self) -> CheckResult:
        """WebSocket 피드 단절: _last_message_time이 60초 이상 경과"""
        ws = self.bot.ws_feed
        if not ws or not getattr(ws, '_running', False):
            return CheckResult("ws_feed", "critical", True, "WS 비활성")

        last_msg = getattr(ws, '_last_message_time', None)
        if last_msg:
            gap = (datetime.now() - last_msg).total_seconds()
            if gap > 60:
                return CheckResult(
                    "ws_feed", "critical", False,
                    f"WS 데이터 {gap:.0f}초 수신 없음",
                )

        return CheckResult("ws_feed", "critical", True, "정상")

    async def _check_daily_loss(self) -> CheckResult:
        """일일 손실 한도 근접: effective_daily_pnl이 한도의 80% 이상"""
        portfolio = self.bot.engine.portfolio
        risk = self.bot.engine.config.risk
        pnl = getattr(portfolio, 'effective_daily_pnl', portfolio.daily_pnl)
        equity = float(portfolio.total_equity)
        if equity <= 0:
            return CheckResult("daily_loss", "critical", True, "자산 0")

        pnl_pct = float(pnl / Decimal(str(equity)) * 100)
        limit_pct = risk.daily_max_loss_pct

        if pnl_pct <= -(limit_pct * 0.8):
            return CheckResult(
                "daily_loss", "critical", False,
                f"일일 손실 {pnl_pct:.1f}% (한도 -{limit_pct}%의 80% 초과)",
                value=pnl_pct,
            )
        return CheckResult(
            "daily_loss", "critical", True,
            f"일일 P&L {pnl_pct:+.1f}%", value=pnl_pct,
        )

    async def _check_pending_deadlock(self) -> CheckResult:
        """Pending 교착: _pending_timestamps에 5분 이상 잔존"""
        rm = self.bot.engine.risk_manager
        if not rm:
            return CheckResult("pending_deadlock", "critical", True, "RM 없음")

        timestamps = getattr(rm, '_pending_timestamps', {})
        now = datetime.now()
        stale = []
        for sym, ts in timestamps.items():
            if (now - ts).total_seconds() > 300:
                stale.append(f"{sym}({(now - ts).total_seconds():.0f}s)")

        if stale:
            return CheckResult(
                "pending_deadlock", "critical", False,
                f"교착 pending: {', '.join(stale)}",
            )
        return CheckResult("pending_deadlock", "critical", True, "정상")

    # ==========================================================
    # Important 체크 (60초 주기)
    # ==========================================================

    async def _check_important(self) -> List[CheckResult]:
        results = []
        results.append(await self._check_memory())
        results.append(await self._check_queue_saturation())
        results.append(await self._check_broker())
        return results

    async def _check_memory(self) -> CheckResult:
        """메모리 사용량: RSS > 500MB 경고"""
        try:
            import psutil
            process = psutil.Process()
            rss_mb = process.memory_info().rss / (1024 * 1024)
            if rss_mb > 500:
                return CheckResult(
                    "memory", "warning", False,
                    f"메모리 {rss_mb:.0f}MB (500MB 초과)", value=rss_mb,
                )
            return CheckResult("memory", "info", True, f"메모리 {rss_mb:.0f}MB", value=rss_mb)
        except ImportError:
            return CheckResult("memory", "info", True, "psutil 미설치")

    async def _check_queue_saturation(self) -> CheckResult:
        """이벤트 큐 포화: 큐 사이즈가 MAX의 80% 이상"""
        engine = self.bot.engine
        qsize = len(engine._event_queue)
        max_size = engine._MAX_QUEUE_SIZE
        ratio = qsize / max_size * 100 if max_size > 0 else 0

        if ratio > 80:
            return CheckResult(
                "queue_saturation", "warning", False,
                f"이벤트 큐 {qsize}/{max_size} ({ratio:.0f}%)", value=ratio,
            )
        return CheckResult(
            "queue_saturation", "info", True,
            f"큐 {qsize}/{max_size}", value=ratio,
        )

    async def _check_broker(self) -> CheckResult:
        """브로커 연결 상태"""
        broker = self.bot.broker
        if not broker:
            return CheckResult("broker", "info", True, "브로커 없음 (dry-run)")
        if not broker.is_connected:
            return CheckResult("broker", "warning", False, "브로커 연결 끊김")
        return CheckResult("broker", "info", True, "연결됨")

    # ==========================================================
    # Periodic 체크 (5분 주기)
    # ==========================================================

    async def _check_periodic(self) -> List[CheckResult]:
        results = []
        results.append(await self._check_rolling_performance())
        return results

    async def _check_rolling_performance(self) -> CheckResult:
        """롤링 성과: 최근 10거래 승률 < 20% 또는 연속 5패 시 경고"""
        journal = self.bot.trade_journal
        if not journal:
            return CheckResult("rolling_perf", "info", True, "저널 없음")

        recent = journal.get_closed_trades(days=3)
        if len(recent) < 5:
            return CheckResult(
                "rolling_perf", "info", True,
                f"최근 거래 {len(recent)}건 (최소 5건 필요)",
            )

        last_10 = recent[-10:] if len(recent) >= 10 else recent
        wins = sum(1 for t in last_10 if getattr(t, 'pnl', 0) > 0)
        win_rate = wins / len(last_10) * 100

        # 연속 손실 카운트
        consec_loss = 0
        for t in reversed(last_10):
            if getattr(t, 'pnl', 0) <= 0:
                consec_loss += 1
            else:
                break

        if win_rate < 20 or consec_loss >= 5:
            return CheckResult(
                "rolling_perf", "warning", False,
                f"승률 {win_rate:.0f}% (최근 {len(last_10)}건), 연속손실 {consec_loss}회",
                value=win_rate,
            )
        return CheckResult(
            "rolling_perf", "info", True,
            f"승률 {win_rate:.0f}% (최근 {len(last_10)}건)", value=win_rate,
        )

    # ==========================================================
    # 알림 처리
    # ==========================================================

    async def _handle_failures(self, failed: List[CheckResult]):
        """실패 항목에 대해 쿨다운 적용 후 텔레그램 알림"""
        now = datetime.now()
        for result in failed:
            cooldown = self._COOLDOWNS.get(result.level, 900)
            last_alert = self._alert_cooldowns.get(result.name)

            if last_alert and (now - last_alert).total_seconds() < cooldown:
                continue

            self._alert_cooldowns[result.name] = now

            emoji = "\U0001f6a8" if result.level == "critical" else "\u26a0\ufe0f"
            try:
                await send_alert(f"{emoji} [HealthCheck] {result.message}")
            except Exception:
                pass

            if result.level == "critical":
                logger.error(f"[HealthMonitor] {result.name}: {result.message}")
            else:
                logger.warning(f"[HealthMonitor] {result.name}: {result.message}")

    # ==========================================================
    # 유틸
    # ==========================================================

    def _is_market_hours(self) -> bool:
        """장중 여부 (프리장~넥스트장 포함: 08:30 ~ 18:00)"""
        from src.core.engine import is_kr_market_holiday

        now = datetime.now()
        if is_kr_market_holiday(now.date()):
            return False
        current = now.strftime("%H:%M")
        return "08:30" <= current <= "18:00"
