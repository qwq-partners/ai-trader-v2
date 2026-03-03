"""
백그라운드 스케줄러 및 주기적 작업 Mixin

run_trader.py의 TradingBot에서 상속하여 사용.
레포트, 진화, 테마 탐지, 스크리닝, 체결 확인, 포트폴리오 동기화 등
백그라운드 루프 메서드를 분리한 모듈.
"""

import asyncio
import aiohttp
import json
import os
import re
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from loguru import logger

from src.core.engine import is_kr_market_holiday
from src.core.event import ThemeEvent, NewsEvent, FillEvent, SignalEvent, MarketDataEvent
from src.core.types import Signal, OrderSide, SignalStrength, StrategyType
from src.data.feeds.kis_websocket import MarketSession
from src.utils.logger import trading_logger, cleanup_old_logs, cleanup_old_cache
from src.utils.telegram import send_alert


class SchedulerMixin:
    """백그라운드 스케줄러 메서드 Mixin (TradingBot에서 상속)"""

    _MAX_WATCH_SYMBOLS = 200  # 감시 종목 최대 수

    def _trim_watch_symbols(self):
        """감시 종목 리스트가 최대 수를 초과하면 오래된 비포지션 종목 제거"""
        if len(self._watch_symbols) <= self._MAX_WATCH_SYMBOLS:
            return
        # 보유 종목은 제거하지 않음
        positions = set(self.engine.portfolio.positions.keys()) if self.engine else set()
        # 초기 config 종목도 보존
        config_syms = set(self.config.get("watch_symbols") or [])
        protected = positions | config_syms
        removable = [s for s in self._watch_symbols if s not in protected]
        excess = len(self._watch_symbols) - self._MAX_WATCH_SYMBOLS
        if excess > 0 and removable:
            to_remove = set(removable[:excess])
            self._watch_symbols = [s for s in self._watch_symbols if s not in to_remove]
            logger.debug(f"[감시 종목] {len(to_remove)}개 정리 → 현재 {len(self._watch_symbols)}개")

    async def _run_pre_market_us_signal(self):
        """US 시장 오버나이트 시그널 사전 조회 (아침 레포트 전)"""
        if not self.us_market_data:
            return

        try:
            signal = await self.us_market_data.get_overnight_signal()
            if not signal:
                return

            sentiment = signal.get("sentiment", "neutral")
            indices = signal.get("indices", {})
            sector_signals = signal.get("sector_signals", {})

            logger.info(f"[US 시그널] 시장 심리: {sentiment}")
            for name, info in indices.items():
                logger.info(f"[US 시그널]   {name}: {info['change_pct']:+.1f}%")

            if sector_signals:
                boosted = [
                    f"{t}({s['boost']:+d})" for t, s in sector_signals.items()
                ]
                logger.info(f"[US 시그널] 한국 테마 영향: {', '.join(boosted)}")
            else:
                logger.info("[US 시그널] 한국 테마 영향 없음 (임계값 미달)")

        except Exception as e:
            logger.warning(f"[US 시그널] 오버나이트 시그널 조회 실패: {e}")

    async def _run_daily_report_scheduler(self):
        """
        일일 레포트 스케줄러

        - 00:00: 일일 통계 초기화
        - 아침: 오늘의 추천 종목 레포트
        - 오후: 추천 종목 결과 레포트
        """
        from src.analytics.daily_report import get_report_generator

        if not self.report_generator:
            self.report_generator = get_report_generator()

        # config에서 스케줄 시간 로드
        sched_cfg = self.config.get("scheduler") or {}
        morning_time_str = sched_cfg.get("morning_report_time", "08:00")
        evening_time_str = sched_cfg.get("evening_report_time", "17:00")
        morning_hour, morning_min = (int(x) for x in morning_time_str.split(":"))
        evening_hour, evening_min = (int(x) for x in evening_time_str.split(":"))

        # 이중 발송 방지: 재시작 후에도 오늘 이미 발송한 레포트는 재발송 안 함
        _report_state_path = Path.home() / ".cache" / "ai_trader" / "report_state.json"

        def _load_report_state() -> dict:
            try:
                if _report_state_path.exists():
                    return json.loads(_report_state_path.read_text())
            except Exception:
                pass
            return {}

        def _save_report_state(state: dict):
            try:
                _report_state_path.parent.mkdir(parents=True, exist_ok=True)
                _report_state_path.write_text(json.dumps(state))
            except Exception:
                pass

        _rs = _load_report_state()
        _today_str = date.today().isoformat()

        last_us_market_report: Optional[date] = (
            date.fromisoformat(_rs.get("us_market_report", ""))
            if _rs.get("us_market_report") == _today_str else None
        )
        last_morning_report: Optional[date] = (
            date.fromisoformat(_rs.get("morning_report", ""))
            if _rs.get("morning_report") == _today_str else None
        )
        last_evening_report: Optional[date] = (
            date.fromisoformat(_rs.get("evening_report", ""))
            if _rs.get("evening_report") == _today_str else None
        )
        last_holiday_refresh_month: Optional[str] = None
        last_daily_reset: Optional[date] = None

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                # 매월 25일 이후: 익월 휴장일 자동 갱신
                if now.day >= 25 and self.kis_market_data:
                    next_month = (now.replace(day=1) + timedelta(days=32)).strftime("%Y%m")
                    if last_holiday_refresh_month != next_month:
                        try:
                            h = await self.kis_market_data.fetch_holidays(next_month)
                            if h:
                                from src.core.engine import set_kr_market_holidays, _kr_market_holidays
                                set_kr_market_holidays(_kr_market_holidays | h)
                                logger.info(f"[휴장일] 익월({next_month}) 휴장일 {len(h)}일 추가 로드")
                            last_holiday_refresh_month = next_month
                        except Exception as e:
                            logger.warning(f"[휴장일] 익월 휴장일 갱신 실패: {e}")

                # 자정: 일일 통계 + 전략 상태 초기화 (공휴일 포함 매일 실행)
                # ⚠️ 재시작 안전장치: 봇 재시작 시 last_daily_reset=None이 되어
                #   장중에도 reset_daily_stats()가 실행되는 버그 방지.
                #   JSON 파일에 오늘 날짜가 이미 있으면 당일 통계 복원 완료 상태 →
                #   리셋 없이 last_daily_reset만 today로 업데이트.
                if last_daily_reset != today:
                    if last_daily_reset is None:
                        # 재시작 여부 확인: JSON에 오늘 날짜가 있으면 이미 초기화됨
                        try:
                            _stats = json.loads(self.engine._DAILY_STATS_PATH.read_text())
                            if _stats.get("date") == today.isoformat():
                                logger.info(
                                    f"[DailyStats] 재시작 감지: 오늘 통계 복원 완료 "
                                    f"(daily_pnl={self.engine.portfolio.daily_pnl:+,.0f}원) → 리셋 생략"
                                )
                                last_daily_reset = today
                        except Exception:
                            pass  # JSON 없거나 파싱 실패 → 아래에서 정상 리셋

                if last_daily_reset != today:
                    try:
                        self.engine.reset_daily_stats()
                        if self.risk_manager:
                            self.risk_manager.reset_daily_stats()

                        # 엔진 RiskManager 일일 상태 초기화
                        if self.engine.risk_manager and hasattr(self.engine.risk_manager, '_stop_loss_today'):
                            self.engine.risk_manager._stop_loss_today.clear()

                        # 전략별 일일 상태 초기화
                        for name, strat in self.strategy_manager.strategies.items():
                            if hasattr(strat, 'clear_gap_stocks'):
                                strat.clear_gap_stocks()
                            if hasattr(strat, 'clear_oversold_stocks'):
                                strat.clear_oversold_stocks()
                            if hasattr(strat, '_theme_entries'):
                                strat._theme_entries.clear()
                            if hasattr(strat, '_active_themes'):
                                strat._active_themes.clear()

                        # 전일 미체결 pending 주문 정리
                        if self.broker:
                            try:
                                pending = await self.broker.get_open_orders()
                                if pending:
                                    logger.info(f"[스케줄러] 전일 미체결 주문 {len(pending)}건 정리")
                                    for order in pending:
                                        try:
                                            await self.broker.cancel_order(order.id)
                                        except Exception as cancel_err:
                                            logger.debug(f"주문 취소 실패 (무시): {cancel_err}")
                            except Exception as e:
                                logger.warning(f"[스케줄러] 미체결 주문 조회 실패 (무시): {e}")
                            # 브로커 내부 pending dict 정리 (조회 실패해도 항상 실행)
                            self.broker._pending_orders.clear()
                            self.broker._order_id_to_kis_no.clear()
                            self.broker._order_id_to_orgno.clear()

                        # ExitManager 매도 pending 및 엔진 RiskManager pending 정리
                        self._exit_pending_symbols.clear()
                        self._exit_pending_timestamps.clear()
                        if self.engine.risk_manager:
                            self.engine.risk_manager._pending_orders.clear()
                            self.engine.risk_manager._pending_quantities.clear()
                            self.engine.risk_manager._pending_timestamps.clear()
                            self.engine.risk_manager._pending_sides.clear()

                        # 엔진 주문 예약 현금 및 폴백 횟수 초기화 (전일 잔여 예약 방지)
                        if hasattr(self.engine, '_reserved_by_order'):
                            self.engine._reserved_by_order.clear()
                        if hasattr(self.engine, '_pending_fallback_count'):
                            self.engine._pending_fallback_count.clear()
                        # risk_manager에도 동일 속성 존재 시 정리
                        if self.engine.risk_manager:
                            if hasattr(self.engine.risk_manager, '_reserved_by_order'):
                                self.engine.risk_manager._reserved_by_order.clear()
                            if hasattr(self.engine.risk_manager, '_pending_fallback_count'):
                                self.engine.risk_manager._pending_fallback_count.clear()

                        # 거래 로거 일일 기록 플러시 및 초기화
                        trading_logger.flush()
                        trading_logger._daily_records.clear()

                        # 종목별 당일 진입 횟수 초기화
                        self._daily_entry_count.clear()

                        # 청산 상태 로그 타임스탬프 초기화 (메모리 누수 방지)
                        if hasattr(self, '_last_exit_status_log'):
                            self._last_exit_status_log.clear()

                        # 주문 실패 알림 초기화 (재실패 시 알림 재발송 위해)
                        if hasattr(self, '_order_fail_alerted'):
                            self._order_fail_alerted.clear()

                        # 매도 차단 종목 + 스크리닝 쿨다운 초기화
                        self._sell_blocked_symbols.clear()
                        self._screening_signal_cooldown.clear()

                        last_daily_reset = today
                        logger.info("[스케줄러] 일일 통계 + 전략 상태 + pending 주문 + 거래로그 초기화 완료")
                    except Exception as e:
                        logger.error(f"[스케줄러] 일일 초기화 실패: {e}")

                # 공휴일(주말 포함)이면 레포트 스킵
                if is_kr_market_holiday(today):
                    await asyncio.sleep(60)
                    continue

                # 미국증시 마감 레포트 (07:00 ~ 07:15)
                if now.hour == 7 and 0 <= now.minute < 15:
                    if last_us_market_report != today:
                        logger.info("[레포트] 미국증시 마감 레포트 발송 시작")
                        try:
                            await self.report_generator.generate_us_market_report(
                                send_telegram=True,
                            )
                            last_us_market_report = today
                            _save_report_state({
                                **_load_report_state(),
                                "us_market_report": today.isoformat(),
                            })
                        except Exception as e:
                            logger.error(f"[레포트] 미국증시 레포트 발송 실패: {e}")

                # 아침 레포트 (설정 시간 ~ +15분)
                if now.hour == morning_hour and morning_min <= now.minute < morning_min + 15:
                    if last_morning_report != today:
                        # US 시장 오버나이트 시그널 선행 조회
                        await self._run_pre_market_us_signal()

                        logger.info("[레포트] 아침 추천 종목 레포트 발송 시작")
                        try:
                            await self.report_generator.generate_morning_report(
                                max_stocks=10,
                                send_telegram=True,
                            )
                            last_morning_report = today
                            _save_report_state({
                                **_load_report_state(),
                                "morning_report": today.isoformat(),
                            })
                        except Exception as e:
                            logger.error(f"[레포트] 아침 레포트 발송 실패: {e}")

                # 오후 결과 레포트 (설정 시간 ~ +15분)
                if now.hour == evening_hour and evening_min <= now.minute < evening_min + 15:
                    if last_evening_report != today:
                        logger.info("[레포트] 오후 결과 레포트 발송 시작")
                        try:
                            await self.report_generator.generate_evening_report(
                                send_telegram=True,
                            )
                            last_evening_report = today
                            _save_report_state({
                                **_load_report_state(),
                                "evening_report": today.isoformat(),
                            })
                        except Exception as e:
                            logger.error(f"[레포트] 오후 레포트 발송 실패: {e}")

                        # 자산 스냅샷 저장 (오후 레포트 직후)
                        equity_tracker = getattr(self, 'equity_tracker', None)
                        if equity_tracker and not getattr(self, '_last_equity_snapshot_date', None) == today:
                            try:
                                name_cache = {}
                                if hasattr(self, 'dashboard') and self.dashboard:
                                    name_cache = self.dashboard.data_collector._build_name_cache()

                                # DB에서 당일 거래 통계 조회
                                db_stats = None
                                tj = self.trade_journal
                                if tj and hasattr(tj, 'pool') and tj.pool:
                                    try:
                                        row = await tj.pool.fetchrow(
                                            "SELECT COUNT(*) as cnt, "
                                            "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins, "
                                            "COALESCE(SUM(pnl), 0) as total_pnl "
                                            "FROM trade_events WHERE event_type='SELL' AND event_time::date=$1",
                                            date.today(),
                                        )
                                        if row and row['cnt'] > 0:
                                            cnt = row['cnt']
                                            db_stats = {
                                                'trades_count': cnt,
                                                'win_rate': round(row['wins'] / cnt * 100, 1),
                                                'realized_pnl': float(row['total_pnl']),
                                            }
                                    except Exception as e:
                                        logger.debug(f"[자산추적] DB 통계 조회 실패: {e}")

                                equity_tracker.save_snapshot(
                                    self.engine.portfolio, self.trade_journal, name_cache, db_stats=db_stats
                                )
                                self._last_equity_snapshot_date = today
                                logger.info("[자산추적] 일일 스냅샷 저장 완료")
                            except Exception as e:
                                logger.error(f"[자산추적] 스냅샷 저장 실패: {e}")

                        # 5일 누적 수급 스코어 갱신 (장 마감 후 1일 1회)
                        if not getattr(self, '_last_supply5d_date', None) == today:
                            try:
                                from src.data.providers.supply_score_provider import SupplyScoreProvider
                                sp = SupplyScoreProvider()
                                await sp.ensure_loaded(force_refresh_today=True)
                                self._last_supply5d_date = today
                                logger.info(
                                    f"[수급5일] 갱신 완료: "
                                    f"{len(sp._loaded_dates)}일치 데이터"
                                )
                            except Exception as _sp_e:
                                logger.warning(f"[수급5일] 갱신 실패: {_sp_e}")

                        # KIS 체결 기반 PnL 보정 (17:00)
                        if not getattr(self, '_last_kis_sync_date', None) == today:
                            try:
                                tj = self.trade_journal
                                if self.broker and hasattr(tj, 'sync_from_kis'):
                                    await tj.sync_from_kis(self.broker, engine=self.engine)
                                    self._last_kis_sync_date = today
                                    logger.info("[KIS동기화] 장 마감 후 체결 동기화 완료")
                            except Exception as e:
                                logger.error(f"[KIS동기화] 장 마감 후 동기화 실패: {e}")

                        # 거래 복기 리포트 생성 (17:00)
                        daily_reviewer = getattr(self, 'daily_reviewer', None)
                        if daily_reviewer and not getattr(self, '_last_trade_report_date', None) == today:
                            try:
                                await daily_reviewer.generate_trade_report(self.trade_journal)
                                self._last_trade_report_date = today
                                logger.info("[거래리뷰] 일일 거래 복기 리포트 생성 완료")
                            except Exception as e:
                                logger.error(f"[거래리뷰] 거래 복기 리포트 생성 실패: {e}")

                # 1분마다 체크
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"레포트 스케줄러 오류: {e}")

    async def _run_evolution_scheduler(self):
        """
        LLM 거래 리뷰 스케줄러

        - 매일 20:30 LLM 종합평가 생성 + 텔레그램 발송
        - 자동 파라미터 변경 비활성화 (리포트만 생성)
        """
        last_review_date: Optional[date] = None

        # config에서 리뷰 실행 시간 로드
        sched_cfg = self.config.get("scheduler") or {}
        evo_time_str = sched_cfg.get("evolution_time", "20:30")
        evo_hour, evo_min = (int(x) for x in evo_time_str.split(":"))

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                # 공휴일(주말 포함)이면 스킵
                if is_kr_market_holiday(today):
                    await asyncio.sleep(60)
                    continue

                # 20:30 ~ +15분: LLM 종합평가 생성
                if now.hour == evo_hour and evo_min <= now.minute < evo_min + 15:
                    if last_review_date != today:
                        daily_reviewer = getattr(self, 'daily_reviewer', None)
                        if daily_reviewer:
                            logger.info("[거래리뷰] LLM 종합평가 생성 시작...")

                            try:
                                result = await daily_reviewer.generate_llm_review(
                                    self.trade_journal
                                )

                                assessment = result.get("assessment", "unknown")
                                trade_count = len(result.get("trade_reviews", []))
                                logger.info(
                                    f"[거래리뷰] LLM 평가 완료: "
                                    f"assessment={assessment}, "
                                    f"거래 {trade_count}건 복기"
                                )

                                last_review_date = today

                            except Exception as e:
                                logger.error(f"[거래리뷰] LLM 평가 생성 실패: {e}")
                                import traceback
                                await self._send_error_alert(
                                    "ERROR",
                                    "LLM 거래 리뷰 생성 오류",
                                    traceback.format_exc()
                                )
                                last_review_date = today
                        else:
                            last_review_date = today

                # 1분마다 체크
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"거래 리뷰 스케줄러 오류: {e}")

    async def _run_weekly_rebalance_scheduler(self):
        """매주 토요일 00:00 전략 예산 리밸런싱"""
        last_rebalance_week: Optional[int] = None

        try:
            while self.running:
                now = datetime.now()

                # 토요일(weekday=5) 00:00~00:15
                if (now.weekday() == 5 and now.hour == 0
                        and 0 <= now.minute < 15):
                    iso_week = now.isocalendar()[1]
                    if last_rebalance_week != iso_week:
                        logger.info("[리밸런싱] 주간 전략 예산 리밸런싱 실행")
                        try:
                            result = await self.strategy_evolver.rebalance_strategy_allocation()
                            last_rebalance_week = iso_week

                            status = result.get("status", "unknown")
                            if status == "applied":
                                before = result.get("before", {})
                                after = result.get("after", {})
                                reasoning = result.get("reasoning", "")

                                lines = [
                                    "📊 <b>주간 전략 예산 리밸런싱</b>",
                                    "",
                                    "<b>■ 변경 내역</b>",
                                ]
                                all_keys = set(list(before.keys()) + list(after.keys()))
                                # 전략명 한글 매핑
                                strat_names = {
                                    "momentum_breakout": "모멘텀",
                                    "sepa_trend": "SEPA",
                                    "rsi2_reversal": "RSI2",
                                    "strategic_swing": "전략스윙",
                                    "theme_chasing": "테마",
                                    "gap_and_go": "갭상승",
                                }
                                for k in sorted(all_keys):
                                    old_v = before.get(k, 0)
                                    new_v = after.get(k, 0)
                                    diff = new_v - old_v
                                    arrow = "🔼" if diff > 0 else "🔽" if diff < 0 else "➡️"
                                    display_name = strat_names.get(k, k)
                                    lines.append(
                                        f"  {arrow} {display_name}: "
                                        f"<b>{old_v:.0f}%</b> → <b>{new_v:.0f}%</b> "
                                        f"({diff:+.1f}%p)"
                                    )
                                if reasoning:
                                    lines.append(f"")
                                    lines.append(f"💡 <b>사유:</b> {reasoning}")

                                await send_alert("\n".join(lines))
                                logger.info(f"[리밸런싱] 완료: {status}")
                            elif status == "skipped":
                                reason = result.get("reason", "")
                                logger.info(f"[리밸런싱] 스킵: {reason}")
                            else:
                                reason = result.get("reason", "")
                                logger.warning(f"[리밸런싱] 결과: {status} - {reason}")

                        except Exception as e:
                            logger.error(f"[리밸런싱] 실행 오류: {e}")
                            import traceback
                            await self._send_error_alert(
                                "ERROR", "주간 리밸런싱 오류",
                                traceback.format_exc()
                            )
                            last_rebalance_week = iso_week

                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"주간 리밸런싱 스케줄러 오류: {e}")

    async def _run_stock_master_refresh(self):
        """
        종목 마스터 갱신 스케줄러

        매일 지정 시간(기본 18:00)에 종목 마스터 DB를 갱신합니다.
        주말은 스킵 옵션 지원.
        """
        sm_cfg = getattr(self, '_stock_master_config', None) or {}
        if not sm_cfg.get("enabled", True):
            logger.info("[종목마스터] 비활성화됨 (stock_master.enabled=false)")
            return

        refresh_time_str = sm_cfg.get("refresh_time", "18:00")
        skip_weekends = sm_cfg.get("skip_weekends", True)
        refresh_hour, refresh_min = (int(x) for x in refresh_time_str.split(":"))
        alert_threshold = sm_cfg.get("alert_on_consecutive_failures", 3)

        last_refresh_date: Optional[date] = None
        consecutive_failures = 0  # 연속 실패 카운터

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                # 주말 스킵
                if skip_weekends and now.weekday() >= 5:
                    await asyncio.sleep(60)
                    continue

                # 지정 시간 ±15분 윈도우
                if (now.hour == refresh_hour
                        and refresh_min <= now.minute < refresh_min + 15
                        and last_refresh_date != today):
                    try:
                        logger.info("[종목마스터] 일일 갱신 시작...")
                        stats = await self.stock_master.refresh_master()
                        if stats:
                            logger.info(
                                f"[종목마스터] 갱신 완료: "
                                f"전체={stats.get('total', 0)}, "
                                f"KOSPI200={stats.get('KOSPI200', 0)}, "
                                f"KOSPI500={stats.get('KOSPI500', 0)}, "
                                f"KOSDAQ150={stats.get('KOSDAQ150', 0)}"
                            )
                            consecutive_failures = 0  # 성공 시 리셋
                        last_refresh_date = today
                    except Exception as e:
                        logger.error(f"[종목마스터] 갱신 오류: {e}")
                        consecutive_failures += 1
                        last_refresh_date = today  # 실패해도 날짜 기록 (무한 재시도 방지)

                        # N일 연속 실패 시 알림
                        if consecutive_failures >= alert_threshold:
                            await self._send_error_alert(
                                "WARNING",
                                f"종목 마스터 {consecutive_failures}일 연속 갱신 실패",
                                f"마지막 오류: {str(e)}\n"
                                f"임계값: {alert_threshold}일\n"
                                f"종목 데이터가 오래되었을 수 있습니다."
                            )

                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[종목마스터] 스케줄러 오류: {e}")

    async def _run_daily_candle_refresh(self):
        """
        일봉 데이터 갱신 스케줄러

        장 마감 후(15:40, 20:40)에 보유 종목 + 후보 종목의 일봉 데이터를 갱신합니다.
        중기 전략(5일+ 보유)의 정확한 캔들 분석을 위해 필수입니다.
        """
        sched_cfg = self.config.get("scheduler") or {}
        refresh_times = sched_cfg.get("candle_refresh_times", ["15:40", "20:40"])
        max_symbols_per_run = sched_cfg.get("candle_refresh_max_symbols", 50)
        skip_weekends = sched_cfg.get("candle_refresh_skip_weekends", True)

        # 시간을 (hour, minute) 튜플 리스트로 변환
        refresh_schedule = []
        for time_str in refresh_times:
            hour, minute = (int(x) for x in time_str.split(":"))
            refresh_schedule.append((hour, minute))

        last_refresh_date: Optional[date] = None
        last_refresh_hour: Optional[int] = None

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                # 공휴일(주말 포함)이면 스킵
                if is_kr_market_holiday(today):
                    await asyncio.sleep(60)
                    continue

                # 주말 스킵 옵션
                if skip_weekends and now.weekday() >= 5:
                    await asyncio.sleep(60)
                    continue

                # 스케줄 시간 체크 (각 시간별 ±10분 윈도우)
                for refresh_hour, refresh_min in refresh_schedule:
                    if (now.hour == refresh_hour
                            and refresh_min <= now.minute < refresh_min + 10
                            and (last_refresh_date != today or last_refresh_hour != refresh_hour)):
                        try:
                            logger.info(f"[일봉갱신] {refresh_hour:02d}:{refresh_min:02d} 스케줄 시작...")

                            # 갱신 대상 종목 수집
                            symbols_to_refresh = []

                            # 1. 보유 종목 (최우선)
                            if self.engine and self.engine.portfolio:
                                position_symbols = list(self.engine.portfolio.positions.keys())
                                symbols_to_refresh.extend(position_symbols)
                                logger.info(f"[일봉갱신] 보유 종목 {len(position_symbols)}개 추가")

                            # 2. 감시 종목 중 상위 점수 (보유 종목 제외)
                            if self.ws_feed and hasattr(self.ws_feed, '_symbol_scores'):
                                # 점수 높은 순 정렬
                                scored_symbols = sorted(
                                    self.ws_feed._symbol_scores.items(),
                                    key=lambda x: x[1],
                                    reverse=True
                                )
                                # 보유 종목 제외하고 상위 N개
                                position_set = set(symbols_to_refresh)
                                candidate_count = 0
                                for symbol, score in scored_symbols:
                                    if symbol not in position_set:
                                        if score >= 70:  # 높은 점수만
                                            symbols_to_refresh.append(symbol)
                                            candidate_count += 1
                                            if len(symbols_to_refresh) >= max_symbols_per_run:
                                                break

                                logger.info(f"[일봉갱신] 후보 종목 {candidate_count}개 추가 (점수 70+)")

                            # 중복 제거
                            symbols_to_refresh = list(dict.fromkeys(symbols_to_refresh))
                            total_symbols = len(symbols_to_refresh)

                            if total_symbols == 0:
                                logger.info("[일봉갱신] 갱신 대상 종목 없음")
                                last_refresh_date = today
                                last_refresh_hour = refresh_hour
                                break

                            # 최대 개수 제한
                            if total_symbols > max_symbols_per_run:
                                symbols_to_refresh = symbols_to_refresh[:max_symbols_per_run]
                                logger.info(
                                    f"[일봉갱신] 대상 종목 {total_symbols}개 → {max_symbols_per_run}개로 제한"
                                )

                            # 일봉 데이터 갱신 (배치)
                            success_count = 0
                            fail_count = 0

                            for symbol in symbols_to_refresh:
                                try:
                                    daily_prices = await self.broker.get_daily_prices(symbol, days=60)
                                    if daily_prices and len(daily_prices) > 0:
                                        success_count += 1
                                        logger.debug(f"[일봉갱신] {symbol}: {len(daily_prices)}일 갱신 완료")
                                    else:
                                        fail_count += 1
                                        logger.debug(f"[일봉갱신] {symbol}: 데이터 없음")

                                    # Rate limit 준수 (0.1초 간격)
                                    await asyncio.sleep(0.1)

                                except Exception as e:
                                    fail_count += 1
                                    logger.debug(f"[일봉갱신] {symbol} 오류: {e}")
                                    await asyncio.sleep(0.1)

                            logger.info(
                                f"[일봉갱신] 완료: 성공={success_count}/{total_symbols}, "
                                f"실패={fail_count}"
                            )

                            last_refresh_date = today
                            last_refresh_hour = refresh_hour

                        except Exception as e:
                            logger.error(f"[일봉갱신] 스케줄 실행 오류: {e}")
                            last_refresh_date = today
                            last_refresh_hour = refresh_hour

                        break  # 한 번만 실행

                # 1분마다 체크
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[일봉갱신] 스케줄러 오류: {e}")

    async def _run_theme_detection(self):
        """테마 탐지 루프"""
        try:
            scan_interval = self.theme_detector.detection_interval_minutes * 60

            while self.running:
                try:
                    # 테마 스캔
                    themes = await self.theme_detector.detect_themes(force=True)

                    if themes:
                        logger.info(f"[테마 탐지] {len(themes)}개 테마 감지")

                        # 테마 이벤트 발행
                        for theme in themes:
                            event = ThemeEvent(
                                source="theme_detector",
                                name=theme.name,
                                score=theme.score,
                                keywords=theme.keywords,
                                symbols=theme.related_stocks,
                            )
                            await self.engine.emit(event)

                            # [스윙 배치 전략] theme_chasing 비활성화 → 테마 종목 WS 구독 제외
                            # logger.debug(f"[테마 탐지] {theme.name} 관련 {len(theme.related_stocks)}종목 (WS 구독 안함)")

                        # 종목별 뉴스 임팩트 → NewsEvent 발행 + WS 구독
                        sentiments = self.theme_detector.get_all_stock_sentiments()
                        for symbol, data in sentiments.items():
                            impact = data.get("impact", 0)
                            direction = data.get("direction", "bullish")
                            reason = data.get("reason", "")
                            abs_impact = abs(impact)

                            # 임팩트 임계값 이상 종목은 NewsEvent 발행
                            # 새 스케일: -10~+10, 임계값 기본 5
                            news_threshold = (self.config.get("scheduler") or {}).get("news_impact_threshold", 5)
                            if abs_impact >= news_threshold:
                                news_event = NewsEvent(
                                    source="theme_detector",
                                    title=reason,
                                    symbols=[symbol],
                                    sentiment=impact / 10.0,  # -1.0 ~ +1.0
                                )
                                await self.engine.emit(news_event)

                                # [스윙 배치 전략] 뉴스 임팩트 종목 WS 구독 제외 (장중 매수 신호 없음)

                except Exception as e:
                    logger.warning(f"테마 스캔 오류: {e}")

                # 감시 종목 정리
                self._trim_watch_symbols()

                # 다음 스캔까지 대기
                await asyncio.sleep(scan_interval)

        except asyncio.CancelledError:
            pass

    async def _run_screening(self):
        """주기적 종목 스크리닝 루프"""
        try:
            # 초기 대기 (다른 컴포넌트 초기화 후)
            await asyncio.sleep(60)

            while self.running:
                try:
                    # 세션 확인 - 마감 시간에는 스크리닝 스킵
                    current_session = self._get_current_session()
                    if current_session == MarketSession.CLOSED:
                        await asyncio.sleep(self._screening_interval)
                        continue

                    logger.info(f"[스크리닝] 동적 종목 스캔 시작... (세션: {current_session.value})")

                    # 통합 스크리닝 실행 (theme_detector 연동)
                    screened = await self.screener.screen_all(
                        theme_detector=self.theme_detector,
                    )

                    # 점수 맵 생성 (WebSocket 우선순위용)
                    scores = {s.symbol: s.score for s in screened}

                    new_symbols = []
                    async with self._watch_symbols_lock:
                        for stock in screened:
                            # 높은 점수 종목만 감시 목록에 추가
                            if stock.score >= 70 and stock.symbol not in self._watch_symbols:
                                new_symbols.append(stock.symbol)
                                self._watch_symbols.append(stock.symbol)
                                logger.info(
                                    f"  [NEW] {stock.symbol} {stock.name}: "
                                    f"점수={stock.score:.0f}, {', '.join(stock.reasons[:2])}"
                                )

                    # [스윙 배치 전략] 장중 신규 매수 신호 없음 →
                    # 스크리닝 결과를 WS/REST에 추가하지 않음 (보유종목만 실시간 수신)
                    # 참고: 스크리닝 자체는 복기/저녁 스캔용으로 유지
                    if new_symbols:
                        logger.info(
                            f"[스크리닝] {len(new_symbols)}개 발굴 (복기용, 장중 WS/REST 구독 제외)"
                        )

                    # 스크리닝 결과 로그 기록 (복기용)
                    if screened:
                        trading_logger.log_screening(
                            source=f"periodic_{current_session.value}",
                            total_stocks=len(screened),
                            top_stocks=[{
                                "symbol": s.symbol,
                                "name": s.name,
                                "score": s.score,
                                "price": s.price,
                                "change_pct": s.change_pct,
                                "reasons": s.reasons,
                            } for s in screened[:20]]
                        )

                    logger.info(f"[스크리닝] 완료 - 총 {len(screened)}개 후보, 신규 {len(new_symbols)}개")

                    # REST 피드용 캐시 (상위 종목)
                    self._last_screened = screened

                except Exception as e:
                    logger.warning(f"스크리닝 오류: {e}", exc_info=True)
                    screened = []

                # === 장중 자동 시그널 발행 (스크리닝과 별도 예외 처리) ===
                # 활성화된 전략만 자동진입 허용 (비활성 전략 우회 방지)
                _enabled = set()
                if hasattr(self, 'strategy_manager') and self.strategy_manager:
                    _enabled = set(self.strategy_manager.enabled_strategies)
                elif hasattr(self, 'engine') and self.engine and hasattr(self.engine, 'strategy_manager'):
                    _enabled = set(self.engine.strategy_manager.enabled_strategies)
                _screening_allowed = bool(_enabled)  # 활성 전략 없으면 스크리닝 진입 차단
                _idx_change = None  # 마켓 레짐 변수 (KOSDAQ 등락률)

                if (screened
                        and _screening_allowed
                        and current_session == MarketSession.REGULAR
                        and self.engine and self.broker
                        and "09:15" <= datetime.now().strftime("%H:%M") <= "15:00"):
                    try:
                        # === 마켓 레짐 필터 (약세장 진입 차단) ===
                        _market_regime_ok = True
                        try:
                            # KODEX 코스닥150(229200)으로 KOSDAQ 레짐 판단
                            _idx_quote = await self.broker.get_quote("229200")
                            _idx_change = _idx_quote.get("change_pct", 0) if _idx_quote else 0
                            if _idx_change <= -1.0:
                                _market_regime_ok = False
                                logger.info(
                                    f"[스크리닝] 마켓 레짐 필터: KOSDAQ {_idx_change:+.1f}% → "
                                    f"약세장 진입 차단"
                                )
                            elif _idx_change <= -0.5:
                                logger.info(
                                    f"[스크리닝] 마켓 레짐 주의: KOSDAQ {_idx_change:+.1f}% → "
                                    f"보수적 진입 (점수 85+ 만)"
                                )
                        except Exception as _mre:
                            logger.debug(f"[스크리닝] 마켓 레짐 조회 실패 (무시): {_mre}")

                        if not _market_regime_ok:
                            pass  # 약세장 → 자동진입 스킵
                        else:
                            # 만료된 쿨다운 정리 (30분)
                            now = datetime.now()
                            expired = [s for s, t in self._screening_signal_cooldown.items()
                                       if (now - t).total_seconds() > 1800]
                            for s in expired:
                                del self._screening_signal_cooldown[s]

                            # 기보유 + pending + 당일 손절 종목
                            held = set(self.engine.portfolio.positions.keys())
                            rm = self.engine.risk_manager
                            pending = set(rm._pending_orders) if rm else set()
                            stopped_today = set(rm._stop_loss_today) if rm and hasattr(rm, '_stop_loss_today') else set()
                            exclude = held | pending | stopped_today

                            # 가용 현금 확인 (포지션 수 제한 없음)
                            available_cash = float(self.engine.get_available_cash())
                            min_pos_value = self.engine.config.risk.min_position_value

                            logger.info(
                                f"[스크리닝] 자동진입 체크: 가용현금={available_cash:,.0f} "
                                f"(보유={len(held)}, pending={len(pending - held)}, 손절차단={len(stopped_today)}), "
                                f"75+후보={sum(1 for s in screened if s.score >= 75)}, "
                                f"제외={len(exclude)}, 쿨다운={len(self._screening_signal_cooldown)}"
                            )

                            if available_cash >= min_pos_value:
                                # 시간대별 등락률 상한 (과열 방지)
                                hour_min = now.strftime("%H:%M")
                                if hour_min < "10:00":
                                    overheating_cap = 12.0   # 장초반
                                elif hour_min >= "13:30":
                                    overheating_cap = 10.0   # 오후
                                else:
                                    overheating_cap = 15.0   # 10:00~13:30: 추세 형성 후

                                max_daily_entries = 2  # 동일 종목 당일 최대 진입 횟수
                                # 마켓 레짐 보수적 모드: KOSDAQ -0.5~-1.0% → 점수 85 이상만
                                _min_score = 85 if (_idx_change is not None and -1.0 < _idx_change <= -0.5) else 75
                                candidates = [
                                    s for s in screened
                                    if s.score >= _min_score
                                    and s.symbol not in exclude
                                    and s.symbol not in self._screening_signal_cooldown
                                    and self._daily_entry_count.get(s.symbol, 0) < max_daily_entries
                                ]

                                # 장중 전략 사전 체크 (전략별 시작시간 반영)
                                _strategy_type = StrategyType.MOMENTUM_BREAKOUT
                                _sched_cfg = self.config.get("momentum_breakout") or {}
                                _momentum_start = _sched_cfg.get("trading_start_time", "09:15")
                                if "momentum_breakout" in _enabled and hour_min >= _momentum_start:
                                    _strategy_type = StrategyType.MOMENTUM_BREAKOUT
                                elif "theme_chasing" in _enabled:
                                    _strategy_type = StrategyType.THEME_CHASING
                                elif "gap_and_go" in _enabled:
                                    _strategy_type = StrategyType.GAP_AND_GO
                                elif "momentum_breakout" in _enabled and hour_min < _momentum_start:
                                    logger.debug(f"[스크리닝] 모멘텀 시작시간({_momentum_start}) 전 → 자동진입 스킵")
                                    candidates = []
                                else:
                                    logger.debug("[스크리닝] 장중 전략 미활성 → 자동진입 스킵")
                                    candidates = []

                                signals_emitted = 0
                                for stock in candidates[:8]:  # 최대 8개 검증 (API 부하 제한)
                                    if signals_emitted >= 5:
                                        break

                                    # 섹터 사전 체크 (불필요한 호가 조회 방지)
                                    _sector = None
                                    if hasattr(self, '_get_sector'):
                                        try:
                                            _sector = await self._get_sector(stock.symbol)
                                        except Exception:
                                            pass
                                    if _sector:
                                        max_per_sector = self.engine.config.risk.max_positions_per_sector
                                        if max_per_sector > 0:
                                            same_sector = sum(1 for p in self.engine.portfolio.positions.values()
                                                             if p.sector == _sector)
                                            if same_sector >= max_per_sector:
                                                logger.debug(
                                                    f"[스크리닝] {stock.symbol} 탈락: 섹터 한도 "
                                                    f"({_sector}: {same_sector}/{max_per_sector})"
                                                )
                                                continue

                                    # 실시간 가격 검증
                                    try:
                                        quote = await self.broker.get_quote(stock.symbol)
                                    except Exception as e:
                                        logger.debug(f"[스크리닝] {stock.symbol} 호가 조회 실패: {e}")
                                        continue
                                    if not quote or quote.get("price", 0) <= 0:
                                        continue

                                    rt_price = quote["price"]
                                    rt_change = quote.get("change_pct", 0)
                                    rt_open = quote.get("open", 0)
                                    rt_volume = quote.get("volume", 0)

                                    # 검증 조건
                                    if rt_change < 1.0:
                                        logger.debug(f"[스크리닝] {stock.symbol} 탈락: 등락률 {rt_change:+.1f}% < 1%")
                                        continue
                                    if rt_change > overheating_cap:
                                        logger.debug(f"[스크리닝] {stock.symbol} 탈락: 과열 {rt_change:+.1f}% > {overheating_cap}%")
                                        continue
                                    if rt_open > 0 and rt_price < rt_open:
                                        logger.debug(f"[스크리닝] {stock.symbol} 탈락: 현재가 {rt_price:,.0f} < 시가 {rt_open:,.0f}")
                                        continue
                                    if rt_volume <= 0:
                                        logger.debug(f"[스크리닝] {stock.symbol} 탈락: 거래량 0")
                                        continue

                                    # 전략 핵심 필터 (generate_signal 우회 보완)
                                    if _strategy_type == StrategyType.MOMENTUM_BREAKOUT:
                                        # 1) vol_ratio 체크 — 수급 있으면 임계 완화
                                        _vol_ratio = 0.0
                                        _vol_match = None
                                        for reason in stock.reasons:
                                            _vol_match = re.search(r"거래량\s*([\d.]+)배", reason)
                                            if _vol_match:
                                                _vol_ratio = float(_vol_match.group(1))
                                                break
                                        # reasons 파싱 실패 시 ScreenedStock.volume_ratio 폴백
                                        if _vol_ratio == 0.0 and stock.volume_ratio > 0:
                                            _vol_ratio = stock.volume_ratio
                                        # 수급 유무 판별 (순매수만, 매도 제외)
                                        _has_supply = any(
                                            ("기관" in r or "외국인" in r) and "매도" not in r
                                            for r in stock.reasons
                                        )
                                        _vol_threshold = 1.5 if _has_supply else 2.5
                                        # vol_ratio 미확인(=0) 시 차단 (거래량 검증 불가)
                                        if _vol_ratio <= 0:
                                            logger.debug(f"[스크리닝] {stock.symbol} 탈락: 거래량 비율 미확인")
                                            continue
                                        if _vol_ratio < _vol_threshold:
                                            logger.debug(
                                                f"[스크리닝] {stock.symbol} 탈락: 거래량 부족 "
                                                f"({_vol_ratio:.1f}배 < {_vol_threshold}배, 수급={'있음' if _has_supply else '없음'})"
                                            )
                                            continue

                                        # 2) MA20 모멘텀 체크
                                        _has_momentum = False
                                        _ma_match = re.search(r"MA20[+]?([\d.]+)%", " ".join(stock.reasons))
                                        if _ma_match:
                                            if float(_ma_match.group(1)) >= 2.0:
                                                _has_momentum = True
                                        if not _has_momentum and rt_change < 3.0:
                                            logger.debug(f"[스크리닝] {stock.symbol} 탈락: 모멘텀 부족 (등락률 {rt_change:+.1f}%)")
                                            continue

                                        # 3) 과열 RSI 체크 (정규식 파싱)
                                        _rsi_blocked = False
                                        _rsi_match = re.search(r"RSI[:\s]*([\d.]+)", " ".join(stock.reasons))
                                        if _rsi_match:
                                            _rsi_val = float(_rsi_match.group(1))
                                            if _rsi_val > 75:
                                                _rsi_blocked = True
                                        if _rsi_blocked:
                                            logger.debug(f"[스크리닝] {stock.symbol} 탈락: RSI 과열 (> 75)")
                                            continue

                                        # 4) 장초반 수급 필터: 11시 전 수급 미확인 종목은 점수 85+ 필수
                                        if now.hour < 11 and not _has_supply:
                                            if stock.score < 85:
                                                logger.debug(
                                                    f"[스크리닝] {stock.symbol} 탈락: 장초반 수급부재 "
                                                    f"(점수 {stock.score:.0f} < 85)"
                                                )
                                                continue

                                    # === 뉴스/공시 검증 ===
                                    _confidence_adj = 0.0
                                    if self._stock_validator:
                                        try:
                                            validation = await self._stock_validator.validate(
                                                symbol=stock.symbol,
                                                stock_name=stock.name,
                                            )
                                            if not validation.approved:
                                                logger.info(
                                                    f"[스크리닝] {stock.symbol} {stock.name} 탈락: "
                                                    f"{validation.block_reason}"
                                                )
                                                continue
                                            _confidence_adj = validation.confidence_adjustment
                                        except Exception as e:
                                            logger.debug(f"[스크리닝] {stock.symbol} 검증 오류 (무시): {e}")

                                    # ATR 기반 stop/target 계산
                                    atr_pct = 4.0  # 기본값
                                    for reason in stock.reasons:
                                        if "ATR:" in reason:
                                            try:
                                                atr_pct = float(reason.split("ATR:")[1].replace("%)", "").strip())
                                            except Exception:
                                                pass

                                    # ATR 1.5x 동적 손절 (최소 2%, 최대 8%), 1:2 리스크:리워드
                                    stop_pct   = min(max(atr_pct * 1.5, 2.0), 8.0)
                                    target_pct = min(max(stop_pct * 2.0, 4.0), 15.0)
                                    stop_price   = rt_price * (1 - stop_pct   / 100)
                                    target_price = rt_price * (1 + target_pct / 100)

                                    signal = Signal(
                                        symbol=stock.symbol,
                                        side=OrderSide.BUY,
                                        strength=SignalStrength.STRONG,
                                        strategy=_strategy_type,
                                        price=Decimal(str(rt_price)),
                                        target_price=Decimal(str(target_price)),
                                        stop_price=Decimal(str(stop_price)),
                                        score=stock.score,
                                        confidence=min(1.0, max(0.0, (stock.score / 100.0) + _confidence_adj)),
                                        reason=f"스크리닝 자동진입: {stock.name} 점수={stock.score:.0f} 등락={rt_change:+.1f}%",
                                        metadata={
                                            "source": "live_screening",
                                            "name": stock.name,
                                            "screening_score": stock.score,
                                            "rt_change_pct": rt_change,
                                            "atr_pct": atr_pct,
                                            "sector": _sector,
                                            "news_validation": _confidence_adj,
                                        },
                                    )

                                    # 종목명 캐시에 저장 (매수 시그널/주문 이벤트에 종목명 표시)
                                    name_cache = getattr(self.engine, '_stock_name_cache', None)
                                    if name_cache is not None and stock.name and stock.name != stock.symbol:
                                        name_cache[stock.symbol] = stock.name

                                    try:
                                        event = SignalEvent.from_signal(signal, source="live_screening")
                                        await self.engine.emit(event)
                                    except Exception as e:
                                        logger.error(f"[스크리닝] {stock.symbol} 시그널 발행 실패: {e}", exc_info=True)
                                        break  # 엔진 에러 시 추가 발행 중단

                                    self._screening_signal_cooldown[stock.symbol] = now
                                    self._daily_entry_count[stock.symbol] = self._daily_entry_count.get(stock.symbol, 0) + 1
                                    signals_emitted += 1

                                    logger.info(
                                        f"[스크리닝] 시그널 발행: {stock.symbol} {stock.name} "
                                        f"점수={stock.score:.0f} 현재가={rt_price:,.0f} 등락={rt_change:+.1f}%"
                                    )

                                    await asyncio.sleep(0.3)  # API rate limit

                                if signals_emitted > 0:
                                    logger.info(f"[스크리닝] 장중 시그널 {signals_emitted}개 발행 완료")

                    except Exception as e:
                        logger.error(f"[스크리닝] 자동진입 오류: {e}", exc_info=True)

                # ── 장중 품질 진입 (intraday_buy Option C) ───────────────────
                _ib_cfg = self.config.get("intraday_buy") or {}
                _ib_enabled = _ib_cfg.get("enabled", False)
                _ib_start   = _ib_cfg.get("trading_start_time", "10:00")
                _ib_end     = _ib_cfg.get("trading_end_time", "14:30")
                _ib_min_score    = float(_ib_cfg.get("min_score", 90))
                _ib_max_change   = float(_ib_cfg.get("max_change_pct", 3.0))
                _ib_min_cash_ratio = float(_ib_cfg.get("min_cash_ratio", 0.20))
                _ib_max_entries  = int(_ib_cfg.get("max_daily_entries", 3))

                if (screened
                        and _ib_enabled
                        and current_session == MarketSession.REGULAR
                        and self.engine and self.broker
                        and _ib_start <= datetime.now().strftime("%H:%M") <= _ib_end):
                    try:
                        _ib_now = datetime.now()
                        _ib_total = float(self.engine.portfolio.total_equity)
                        _ib_cash  = float(self.engine.get_available_cash())
                        _ib_cash_ratio = _ib_cash / _ib_total if _ib_total > 0 else 0

                        if _ib_cash_ratio < _ib_min_cash_ratio:
                            logger.debug(
                                f"[장중품질] 현금 부족: {_ib_cash_ratio:.1%} "
                                f"< {_ib_min_cash_ratio:.1%} → 스킵"
                            )
                        else:
                            _ib_held    = set(self.engine.portfolio.positions.keys())
                            _ib_rm      = self.engine.risk_manager
                            _ib_stopped = set(_ib_rm._stop_loss_today) if _ib_rm and hasattr(_ib_rm, '_stop_loss_today') else set()
                            _ib_exclude = _ib_held | _ib_stopped

                            # 오늘 장중품질 진입 카운터 초기화
                            if not hasattr(self, '_ib_daily_count'):
                                self._ib_daily_count = {}
                            _ib_today_key = _ib_now.date().isoformat()
                            _ib_today_cnt = self._ib_daily_count.get(_ib_today_key, 0)

                            # 후보 필터: 점수 ≥ 90 · 등락 0~3% · 미보유 · 쿨다운 없음
                            _ib_candidates = [
                                s for s in screened
                                if s.score >= _ib_min_score
                                and 0.0 <= s.change_pct <= _ib_max_change
                                and s.symbol not in _ib_exclude
                                and s.symbol not in self._screening_signal_cooldown
                            ]

                            logger.info(
                                f"[장중품질] 후보 {len(_ib_candidates)}개 | "
                                f"현금={_ib_cash_ratio:.1%} ({_ib_cash:,.0f}) | "
                                f"오늘진입={_ib_today_cnt}/{_ib_max_entries}"
                            )

                            # llm_verify_enabled 루프 외부에서 1회만 읽기 (P2 #4)
                            _ib_llm_verify_on = _ib_cfg.get("llm_verify_enabled", False)

                            for _ib_stock in _ib_candidates[:5]:
                                if _ib_today_cnt >= _ib_max_entries:
                                    break

                                # RSI 과열 체크 (ScreenedStock.rsi 전용 필드 사용 — reasons 파싱 불필요)
                                _ib_rsi = _ib_stock.rsi
                                if _ib_rsi is not None and _ib_rsi > 75:
                                    logger.info(f"[장중품질] {_ib_stock.symbol} 탈락: RSI 과열 ({_ib_rsi:.1f})")
                                    continue
                                if _ib_rsi is None:
                                    logger.debug(f"[장중품질] {_ib_stock.symbol} RSI 데이터 없음 → 과열 체크 스킵")

                                # 수급 확인 (외국인 or 기관)
                                if not (_ib_stock.has_foreign_buying or _ib_stock.has_inst_buying):
                                    logger.info(f"[장중품질] {_ib_stock.symbol} 탈락: 수급 미확인")
                                    continue

                                # 실시간 가격 및 등락률 재확인
                                try:
                                    _ib_quote = await self.broker.get_quote(_ib_stock.symbol)
                                except Exception as _qe:
                                    logger.debug(f"[장중품질] {_ib_stock.symbol} 호가 조회 실패: {_qe}")
                                    continue
                                if not _ib_quote or _ib_quote.get("price", 0) <= 0:
                                    continue

                                _ib_rt_price  = _ib_quote["price"]
                                _ib_rt_change = _ib_quote.get("change_pct", 0)

                                if not (0.0 <= _ib_rt_change <= _ib_max_change):
                                    logger.info(
                                        f"[장중품질] {_ib_stock.symbol} 탈락: "
                                        f"실시간 등락 {_ib_rt_change:+.1f}% ≠ 0~{_ib_max_change}%"
                                    )
                                    continue

                                # 뉴스/공시 검증
                                if self._stock_validator:
                                    try:
                                        _ib_val = await self._stock_validator.validate(
                                            symbol=_ib_stock.symbol,
                                            stock_name=_ib_stock.name,
                                        )
                                        if not _ib_val.approved:
                                            logger.info(
                                                f"[장중품질] {_ib_stock.symbol} 탈락: {_ib_val.block_reason}"
                                            )
                                            continue
                                    except Exception:
                                        pass

                                # ── LLM 2차 검증 (arXiv:2602.23330 fine-grained verification) ─
                                # 설정: intraday_buy.llm_verify_enabled: true 시 활성
                                # 점수 95+ 후보에 대해 Gemini Flash로 3개 체크리스트 검증
                                # LLM 실패 시 fall-through (신호 차단 안 함)
                                if _ib_llm_verify_on and _ib_stock.score >= 95:
                                    try:
                                        _ib_llm_ok = await self._llm_verify_intraday(
                                            stock=_ib_stock,
                                            rt_price=_ib_rt_price,
                                            rt_change=_ib_rt_change,
                                        )
                                        if not _ib_llm_ok:
                                            logger.info(
                                                f"[장중품질] {_ib_stock.symbol} LLM 2차검증 탈락"
                                            )
                                            continue
                                    except Exception as _llm_e:
                                        logger.debug(f"[장중품질] LLM 검증 오류 → 스킵: {_llm_e}")

                                # ATR 기반 손절/목표가
                                _ib_atr = 4.0
                                for _r in _ib_stock.reasons:
                                    if "ATR:" in _r:
                                        try:
                                            _ib_atr = float(_r.split("ATR:")[1].replace("%)", "").strip())
                                        except Exception:
                                            pass
                                # ATR 1.5x 동적 손절 (최소 2%, 최대 8%), 1:2 리스크:리워드
                                _ib_stop_pct   = min(max(_ib_atr * 1.5, 2.0), 8.0)
                                _ib_target_pct = min(max(_ib_stop_pct * 2.0, 4.0), 15.0)
                                _ib_stop   = _ib_rt_price * (1 - _ib_stop_pct   / 100)
                                _ib_target = _ib_rt_price * (1 + _ib_target_pct / 100)

                                _ib_signal = Signal(
                                    symbol=_ib_stock.symbol,
                                    side=OrderSide.BUY,
                                    strength=SignalStrength.STRONG,
                                    strategy=StrategyType.SEPA_TREND,
                                    price=Decimal(str(_ib_rt_price)),
                                    target_price=Decimal(str(_ib_target)),
                                    stop_price=Decimal(str(_ib_stop)),
                                    score=_ib_stock.score,
                                    confidence=min(1.0, _ib_stock.score / 100.0),
                                    reason=(
                                        f"장중품질진입: {_ib_stock.name} "
                                        f"점수={_ib_stock.score:.0f} 등락={_ib_rt_change:+.1f}%"
                                    ),
                                    metadata={
                                        "source": "intraday_quality",
                                        "name": _ib_stock.name,
                                        "screening_score": _ib_stock.score,
                                        "rt_change_pct": _ib_rt_change,
                                        "atr_pct": _ib_atr,
                                    },
                                )

                                # 종목명 캐시
                                _nc = getattr(self.engine, '_stock_name_cache', None)
                                if _nc is not None and _ib_stock.name:
                                    _nc[_ib_stock.symbol] = _ib_stock.name

                                _ib_event = SignalEvent.from_signal(_ib_signal, source="intraday_quality")
                                await self.engine.emit(_ib_event)

                                # 쿨다운 등록 (30분) + 카운터 갱신
                                self._screening_signal_cooldown[_ib_stock.symbol] = _ib_now
                                _ib_today_cnt += 1
                                self._ib_daily_count[_ib_today_key] = _ib_today_cnt

                                logger.info(
                                    f"[장중품질] ✅ 신호 발행: {_ib_stock.symbol} {_ib_stock.name} "
                                    f"점수={_ib_stock.score:.0f} 등락={_ib_rt_change:+.1f}% "
                                    f"가격={_ib_rt_price:,} ({_ib_today_cnt}/{_ib_max_entries})"
                                )
                                await asyncio.sleep(0.3)

                    except Exception as _ib_e:
                        logger.warning(f"[장중품질] 오류: {_ib_e}", exc_info=True)

                # 다음 스캔까지 대기
                await asyncio.sleep(self._screening_interval)

        except asyncio.CancelledError:
            pass

    async def _llm_verify_intraday(
        self,
        stock,
        rt_price: float,
        rt_change: float,
        timeout_sec: float = 8.0,  # Gemini Flash 실제 응답 평균 3~5초 → 8초 여유
    ) -> bool:
        """장중품질 LLM 2차 검증 (arXiv:2602.23330 fine-grained verification)

        Gemini Flash를 사용해 진입 후보의 모멘텀 품질을 3-항목 체크리스트로 검증.
        응답이 없거나 LLM 오류 발생 시 True 반환 (fall-through, 신호 차단 안 함).

        설정: intraday_buy.llm_verify_enabled: true 로 활성화
        비용: Gemini Flash (초경량 모델), 호출당 ~0.1¢ 수준

        [프롬프트 설계 원칙] 사전 필터(등락 0~3%, 장중 시간)와 중복되는 항목은 제외.
        LLM 고유 판단 영역에 집중:
          Q1: 선정 근거의 질 — 의미 있는 수급/모멘텀 신호인가? (단순 거래량 급등과 구별)
          Q2: 추세 지속 가능성 — 단기 급등 후 꺾임 패턴이 아닌가?
          Q3: 진입 위험 — 공시·뉴스 없이 급등하는 불명확 상승인가?
        """
        try:
            from src.utils.llm import get_llm_manager, LLMTask

            llm = get_llm_manager()
            reasons_text = " | ".join(stock.reasons[:5]) if stock.reasons else "없음"

            # ── 시장 레짐 감지 (전문가 패널 캐시 우선, 없으면 ETF 대리) ──────
            regime = "neutral"
            try:
                from src.signals.strategic.expert_panel import ExpertPanel
                _panel = ExpertPanel()
                _outlook = _panel.load_outlook()
                if _outlook:
                    regime = _outlook.market_regime  # "bullish" | "neutral" | "bearish"
            except Exception:
                pass

            # 레짐별 질문 및 통과 기준 설정
            if regime == "bullish":
                _q_block = """1. 수급 품질: 외국인/기관 매수 근거가 명확한가? (스마트머니 유입 신호)
2. 추세 지속성: 신고가 접근 / 섹터 상대강도 강세 근거가 있는가?
3. 리스크 부재: 과열·고점소진·공시 리스크 없는가?"""
                _pass_rule = "q1=Y이고 (q2=Y 또는 q3=Y)이면 pass"
                _sys_note = "강세장 기준: 수급+추세 또는 수급+리스크 2항목 이상 통과 시 pass."
            elif regime == "bearish":
                _q_block = """1. 하방 방어성: 약세장에서도 외국인/기관이 지속 매집 중이고, 섹터가 시장 대비 강한가?
2. 수급 지속성: 외국인 또는 기관 순매수가 3일 이상 연속이며 단발성이 아닌가?
3. 리스크 부재: "섹터쏠림감점", "섹터하위", "과열", "고점소진" 등 부정 태그가 없는가?"""
                _pass_rule = "q1=Y AND q2=Y AND q3=Y 모두 통과해야 pass (약세장 엄격 기준)"
                _sys_note = "약세장 기준: 3항목 모두 Y여야 pass. 하나라도 N이면 false."
            else:  # neutral
                _q_block = """1. 수급 품질: 선정 근거에 외국인/기관 매수 관련 내용이 있고 스마트머니 유입 근거가 있는가?
2. 추세 건전성: 지속 매집 패턴(섹터상대강도, 모멘텀지속, 신고가)이며 단기 급등 꺾임 징후가 없는가?
3. 리스크 부재: "섹터쏠림감점", "과열" 같은 부정 태그가 없고 진입 위험 요소가 없는가?"""
                _pass_rule = "q1=Y이고 q2=Y 또는 q3=Y이면 pass"
                _sys_note = "중립장 기준: 수급(q1) 필수 + 추세/리스크 중 1개 이상 통과."

            prompt = f"""장중 주식 진입 검증 (한국 주식, {datetime.now().strftime('%H:%M')} 기준, 시장레짐={regime})

종목: {stock.name} ({stock.symbol})
현재가: {rt_price:,}원 | 등락: {rt_change:+.1f}% | 스크리닝점수: {stock.score:.0f}점
선정 근거: {reasons_text}

아래 3가지 항목을 Y/N으로만 판단하세요 (선정 근거 텍스트 기반):

{_q_block}

통과 기준: {_pass_rule}

응답 형식 (JSON만):
{{"q1": "Y", "q2": "Y", "q3": "Y", "pass": true}}"""

            result = await asyncio.wait_for(
                llm.complete_json(
                    prompt=prompt,
                    system=f"당신은 한국 주식 장중 진입 검증 전문가입니다. {_sys_note} 선정 근거 텍스트만 보고 JSON으로만 응답하세요.",
                    task=LLMTask.QUICK_ANALYSIS,
                    max_tokens=120,
                ),
                timeout=timeout_sec,
            )

            if not result or not isinstance(result, dict):
                logger.debug(f"[LLM검증] {stock.symbol} 응답 없음 → pass")
                return True

            q1 = result.get("q1", "Y")
            q2 = result.get("q2", "Y")
            q3 = result.get("q3", "Y")

            # 레짐별 통과 판정 (LLM 응답의 "pass"도 참고하되, 규칙 우선)
            if regime == "bearish":
                passed = (q1 == "Y" and q2 == "Y" and q3 == "Y")
            else:  # bullish, neutral
                passed = (q1 == "Y" and (q2 == "Y" or q3 == "Y"))

            logger.info(
                f"[LLM검증] {stock.symbol} {stock.name} [{regime}]: "
                f"q1={q1} q2={q2} q3={q3} → {'✅통과' if passed else '❌탈락'}"
            )
            return passed

        except asyncio.TimeoutError:
            logger.debug(f"[LLM검증] {stock.symbol} 타임아웃 → pass")
            return True
        except Exception as e:
            logger.debug(f"[LLM검증] {stock.symbol} 오류({e}) → pass")
            return True

    async def _run_rest_price_feed(self):
        """REST 폴링 시세 피드 (WebSocket 미사용 시 전략/청산 활성화)

        45초 주기로 보유 포지션 + 스크리닝 상위 종목의 시세를 REST API 조회 →
        MarketDataEvent 생성 → 엔진 emit → 모든 전략(momentum, theme, gap, exit) 활성화.
        """
        try:
            # 초기 대기 (스크리닝과 시간 분산)
            await asyncio.sleep(90)

            while self.running:
                try:
                    current_session = self._get_current_session()
                    if current_session == MarketSession.CLOSED:
                        await asyncio.sleep(45)
                        continue

                    # 대상 종목: 보유 포지션만 (WS 백업)
                    # [스윙 배치 전략] 장중 신규 매수 없음 → 스크리닝 종목 REST 폴링 제외
                    ws_covered = set()
                    if self.ws_feed and self.ws_feed._connected:
                        ws_covered = self.ws_feed._subscribed_symbols

                    # WS가 커버 못 하는 보유종목만 REST로 백업 폴링
                    target_symbols = [
                        s for s in self.engine.portfolio.positions.keys()
                        if s.zfill(6) not in ws_covered
                    ]

                    if not target_symbols:
                        await asyncio.sleep(45)
                        continue

                    success_count = 0
                    for symbol in target_symbols:
                        try:
                            quote = await self.broker.get_quote(symbol)
                            if not quote or quote.get("price", 0) <= 0:
                                continue

                            price = quote["price"]
                            event = MarketDataEvent(
                                symbol=symbol,
                                open=Decimal(str(quote.get("open", price))),
                                high=Decimal(str(quote.get("high", price))),
                                low=Decimal(str(quote.get("low", price))),
                                close=Decimal(str(price)),
                                volume=quote.get("volume", 0),
                                change_pct=quote.get("change_pct", 0.0),
                                prev_close=Decimal(str(quote["prev_close"])) if quote.get("prev_close") else None,
                                source="rest_polling",
                            )
                            await self.engine.emit(event)

                            # 보유 종목 ExitManager 청산 체크
                            if self.exit_manager and symbol in self.engine.portfolio.positions:
                                await self._check_exit_signal(symbol, Decimal(str(price)))

                            success_count += 1
                        except Exception as e:
                            logger.debug(f"[REST피드] {symbol} 시세 조회 실패: {e}")

                        await asyncio.sleep(0.15)  # API rate limit (초당 ~6건)

                    if success_count > 0:
                        ws_info = f", WS={len(ws_covered)}종목" if ws_covered else ""
                        logger.info(
                            f"[REST피드] 보유종목 WS백업 {success_count}/{len(target_symbols)}개 갱신 "
                            f"(세션={current_session.value}{ws_info})"
                        )

                except Exception as e:
                    logger.warning(f"[REST피드] 오류: {e}", exc_info=True)

                await asyncio.sleep(45)

        except asyncio.CancelledError:
            pass

    async def _run_fill_check(self):
        """체결 확인 루프 (적응형 폴링: 미체결 유무에 따라 2초/5초)"""
        check_interval = 5  # 초 (기본값)
        self._fill_check_errors = 0  # 연속 네트워크 오류 카운터 초기화

        try:
            while self.running:
                try:
                    # 미체결 주문이 있는 경우에만 확인
                    open_orders = await self.broker.get_open_orders()

                    if open_orders:
                        fills = await self.broker.check_fills()

                        for fill in fills:
                            logger.info(
                                f"[체결] {fill.symbol} {fill.side.value} "
                                f"{fill.quantity}주 @ {fill.price:,.0f}원"
                            )

                            # 체결 이벤트 발행 → _on_fill() 핸들러에서 일괄 처리
                            event = FillEvent.from_fill(fill, source="kis_broker")
                            await self.engine.emit(event)

                    # 미체결 주문 유무에 따라 폴링 간격 조정
                    check_interval = 2 if open_orders else 5

                    # 성공 시 에러 카운터 리셋
                    if self._fill_check_errors > 0:
                        self._fill_check_errors = 0

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"체결 확인 네트워크 오류: {e}")
                    self._fill_check_errors += 1
                    if self._fill_check_errors >= 3:
                        # 토큰 만료 가능성 → 갱신 시도
                        if self.broker:
                            await self.broker._ensure_token()
                        await self._send_error_alert(
                            "ERROR",
                            f"체결 확인 연속 네트워크 오류 ({self._fill_check_errors}회)",
                            str(e)
                        )
                        self._fill_check_errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"체결 확인 오류: {e}")
                    self._fill_check_errors += 1
                    if self._fill_check_errors >= 5:
                        await self._send_error_alert(
                            "ERROR",
                            f"체결 확인 연속 오류 ({self._fill_check_errors}회)",
                            str(e)
                        )
                        self._fill_check_errors = 0

                await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            pass

    async def _sync_portfolio(self):
        """KIS API와 포트폴리오 동기화"""
        if not self.broker:
            return

        try:
            # 1. KIS API에서 실제 잔고/포지션 조회 (lock 밖에서 수행 - IO 작업)
            balance = await self.broker.get_account_balance()
            kis_positions = await self.broker.get_positions()

            if not balance:
                logger.warning("포트폴리오 동기화: 잔고 조회 실패")
                return

            # 2. API 빈 결과 방어: lock 밖에서 재시도 (lock 내 sleep 방지)
            bot_symbols = set(self.engine.portfolio.positions.keys())
            kis_symbols = set(kis_positions.keys()) if kis_positions else set()
            if bot_symbols and not kis_symbols:
                logger.warning(
                    "[동기화] KIS 포지션 조회 결과 0건 (봇 보유 "
                    f"{len(bot_symbols)}건) → 5초 후 재시도"
                )
                await asyncio.sleep(5)
                kis_positions = await self.broker.get_positions()
                kis_symbols = set(kis_positions.keys()) if kis_positions else set()
                if bot_symbols and not kis_symbols:
                    logger.warning(
                        "[동기화] 재시도에도 KIS 포지션 0건 → API 오류로 간주, 동기화 건너뜀"
                    )
                    return

            # 3. lock 내에서 포트폴리오 수정 (다른 태스크와 동시 접근 방지)
            async with self._portfolio_lock:
                portfolio = self.engine.portfolio
                kis_symbols = set(kis_positions.keys()) if kis_positions else set()
                bot_symbols = set(portfolio.positions.keys())

                # 유령 포지션 제거 (봇에만 있고 KIS에 없는 종목)
                ghost_symbols = bot_symbols - kis_symbols
                for symbol in ghost_symbols:
                    pos = portfolio.positions[symbol]
                    logger.warning(
                        f"[동기화] 유령 포지션 제거: {symbol} {pos.name} "
                        f"({pos.quantity}주 @ {pos.avg_price:,.0f}원)"
                    )
                    del portfolio.positions[symbol]
                    if self.exit_manager and hasattr(self.exit_manager, '_states'):
                        self.exit_manager._states.pop(symbol, None)
                    # 관련 pending/차단 상태도 함께 정리
                    self._exit_pending_symbols.discard(symbol)
                    self._exit_pending_timestamps.pop(symbol, None)
                    self._sell_blocked_symbols.pop(symbol, None)

                # 누락 포지션 추가 (KIS에 있고 봇에 없는 종목)
                new_symbols = kis_symbols - bot_symbols
                if new_symbols:
                    # DB에서 전략/진입시간 복원
                    new_positions = {s: kis_positions[s] for s in new_symbols}
                    await self._restore_position_metadata(new_positions)

                for symbol in new_symbols:
                    pos = kis_positions[symbol]
                    # DB 복원 실패 시 메모리 캐시에서 전략 복원
                    if not pos.strategy and symbol in self._symbol_strategy:
                        pos.strategy = self._symbol_strategy[symbol]
                    portfolio.positions[symbol] = pos
                    logger.info(
                        f"[동기화] 포지션 추가: {symbol} {pos.name} "
                        f"({pos.quantity}주 @ {pos.avg_price:,.0f}원, "
                        f"전략={pos.strategy or '?'})"
                    )
                    if self.exit_manager:
                        self.exit_manager.register_position(pos)
                    if symbol not in self._watch_symbols:
                        self._watch_symbols.append(symbol)

                # 기존 포지션 수량/가격 업데이트
                common_symbols = bot_symbols & kis_symbols
                for symbol in common_symbols:
                    bot_pos = portfolio.positions[symbol]
                    kis_pos = kis_positions[symbol]
                    if bot_pos.quantity != kis_pos.quantity:
                        logger.warning(
                            f"[동기화] 수량 수정: {symbol} "
                            f"{bot_pos.quantity}주 → {kis_pos.quantity}주"
                        )
                        bot_pos.quantity = kis_pos.quantity
                    if kis_pos.avg_price > 0 and bot_pos.avg_price != kis_pos.avg_price:
                        logger.info(
                            f"[동기화] 평단가 수정: {symbol} "
                            f"{bot_pos.avg_price:,.0f}원 → {kis_pos.avg_price:,.0f}원"
                        )
                        bot_pos.avg_price = kis_pos.avg_price
                    if kis_pos.current_price > 0:
                        bot_pos.current_price = kis_pos.current_price

                # 현금 동기화
                available_cash = Decimal(str(balance.get('available_cash', 0)))
                if available_cash > 0:
                    old_cash = portfolio.cash
                    portfolio.cash = available_cash
                    if abs(old_cash - available_cash) > 1000:
                        logger.info(
                            f"[동기화] 현금 수정: {old_cash:,.0f}원 → {available_cash:,.0f}원"
                        )

                # lock 안에서 로깅 값 캡처 (lock 해제 후 데이터 불일치 방지)
                _log_ghost = len(ghost_symbols)
                _log_new = len(new_symbols)
                _log_total = len(portfolio.positions)
                _log_cash = float(portfolio.cash)
                _log_equity = float(portfolio.total_equity)

            changes = _log_ghost + _log_new
            if changes > 0:
                logger.info(
                    f"[동기화] 완료: 제거={_log_ghost}, "
                    f"추가={_log_new}, "
                    f"보유={_log_total}종목"
                )
                trading_logger.log_portfolio_sync(
                    ghost_removed=_log_ghost,
                    new_added=_log_new,
                    total_positions=_log_total,
                    cash=_log_cash,
                    total_equity=_log_equity,
                )
            else:
                logger.debug(
                    f"[동기화] 확인 완료: 보유={_log_total}종목, 변경 없음"
                )

        except Exception as e:
            logger.error(f"포트폴리오 동기화 오류: {e}")

    async def _run_portfolio_sync(self):
        """주기적 포트폴리오 동기화 루프"""
        await asyncio.sleep(30)  # 시작 후 30초 대기
        while self.running:
            try:
                await self._sync_portfolio()
            except Exception as e:
                logger.error(f"동기화 루프 오류: {e}")
            await asyncio.sleep(120)  # 2분마다 동기화 (KIS API 응답 지연 대응)

    async def _run_strategic_prescan(self):
        """15:35 전략적 사전분석 (배치 스캔 직전 수급 추세 + VCP 탐지)"""
        logger.info("[전략적분석] ===== 사전분석 시작 =====")
        try:
            from src.signals.strategic.supply_trend import SupplyTrendDetector
            from src.signals.strategic.vcp_detector import VCPDetector

            # 수급 추세 탐지
            supply_detector = SupplyTrendDetector(
                kis_market_data=getattr(self, 'kis_market_data', None)
            )
            supply_results = await supply_detector.detect_accumulation()
            logger.info(f"[전략적분석] 수급 추세 {len(supply_results)}종목 탐지")

            # VCP 탐지: swing_screener 데이터가 있으면 재사용
            vcp_detector = VCPDetector()
            screener = getattr(self, 'batch_analyzer', None)
            if screener and hasattr(screener, '_screener'):
                # batch_analyzer._screener가 마지막 스캔에서 캐시한 데이터 재사용
                # → 실제로는 별도 스캔이 필요할 수 있으므로 간이 스캔
                pass

            # VCP는 FDR 데이터 필요 — 스캔 시 swing_screener가 _apply_strategic_overlay에서
            # 캐시된 VCP를 로드하므로, 여기서는 수급 추세만 선행 실행
            logger.info("[전략적분석] 사전분석 완료 (VCP는 배치 스캔 시 통합)")

        except Exception as e:
            logger.error(f"[전략적분석] 사전분석 오류: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _run_expert_panel(self):
        """일요일 21:00 주간 전문가 패널"""
        logger.info("[전문가패널] ===== 주간 분석 시작 =====")
        try:
            from src.signals.strategic.data_collector import StrategicDataCollector
            from src.signals.strategic.expert_panel import ExpertPanel

            data_collector = StrategicDataCollector(
                kis_market_data=getattr(self, 'kis_market_data', None),
                theme_detector=getattr(self, 'theme_detector', None),
            )
            panel = ExpertPanel(data_collector=data_collector)
            outlook = await panel.run_weekly_analysis()

            if outlook:
                # 텔레그램 알림 (HTML)
                stocks = outlook.recommended_stocks
                high_conviction = [s for s in stocks if s.conviction >= 0.5]

                regime_emoji = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}.get(
                    outlook.market_regime, "⚪"
                )
                regime_kr = {"bullish": "강세", "neutral": "중립", "bearish": "약세"}.get(
                    outlook.market_regime, outlook.market_regime
                )

                lines = [
                    f"📊 <b>주간 전문가 패널 분석</b>",
                    f"",
                    f"{regime_emoji} 시장 레짐: <b>{regime_kr}</b>",
                    f"추천 종목: <b>{len(stocks)}개</b> (고확신 {len(high_conviction)}개)",
                    f"",
                    f"<b>■ 고확신 추천 종목</b>",
                ]
                for i, s in enumerate(high_conviction[:7], 1):
                    conv_bar = "●" * int(s.conviction * 4) + "○" * (4 - int(s.conviction * 4))
                    lines.append(
                        f"  {i}. <b>{s.name}</b> <code>{s.symbol}</code>"
                    )
                    horizon_kr = {
                        "1M": "1개월", "3M": "3개월", "6M": "6개월", "1Y": "1년",
                    }.get(s.horizon, s.horizon)
                    lines.append(
                        f"      {horizon_kr} | 확신 {conv_bar} {s.conviction:.0%}"
                    )
                    if s.reasons:
                        lines.append(f"      → {s.reasons[0]}")

                if outlook.risk_factors:
                    lines.append(f"")
                    lines.append(f"<b>■ 주요 리스크</b>")
                    for rf in outlook.risk_factors[:4]:
                        lines.append(f"  ⚠️ {rf}")

                msg = "\n".join(lines)
                await send_alert(msg)
                logger.info(f"[전문가패널] 완료: {len(stocks)}종목 추천")
            else:
                logger.warning("[전문가패널] 결과 없음")

        except Exception as e:
            logger.error(f"[전문가패널] 실행 오류: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _run_batch_scheduler(self):
        """
        스윙 모멘텀 배치 스케줄러

        [아침 스캔 모드 - morning_scan_enabled=true (기본)]
        - 08:15 전략적 사전분석 (수급 추세 + VCP)
        - 08:20 아침 스캔 (전일 종가 + 미국 오버나이트 반영)
        - 09:01 시그널 실행 (장 시작 후)
        - 09:30~15:20 매 30분 포지션 모니터링
        - 일요일 21:00 전문가 패널 (주 1회)

        [전일 마감 후 스캔 모드 - morning_scan_enabled=false]
        - 15:35 전략적 사전분석 (수급 추세 + VCP)
        - 15:40 일일 스캔 (장 마감 후)
        - 19:30 저녁 스캔 (넥스트장 반영 2차 보정)
        - 09:01 시그널 실행 (장 시작 후)
        """
        if not hasattr(self, 'batch_analyzer') or not self.batch_analyzer:
            logger.info("[배치스케줄러] batch_analyzer 없음, 스킵")
            return

        # config에서 스케줄 시간 로드
        batch_cfg = self.config.get("batch") or {}
        scan_time_str = batch_cfg.get("daily_scan_time", "15:40")
        execute_time_str = batch_cfg.get("execute_time", "09:01")
        monitor_interval = batch_cfg.get("position_update_interval", 30)  # 분
        evening_scan_enabled = batch_cfg.get("evening_scan_enabled", True)
        evening_scan_time_str = batch_cfg.get("evening_scan_time", "19:30")

        # 아침 스캔 설정 (morning_scan_enabled=true 시 15:40/19:30 대체)
        morning_scan_enabled = batch_cfg.get("morning_scan_enabled", False)
        morning_scan_time_str = batch_cfg.get("morning_scan_time", "08:20")
        morning_hour, morning_min = (int(x) for x in morning_scan_time_str.split(":"))

        scan_hour, scan_min = (int(x) for x in scan_time_str.split(":"))
        exec_hour, exec_min = (int(x) for x in execute_time_str.split(":"))
        evening_hour, evening_min = (int(x) for x in evening_scan_time_str.split(":"))

        # 전략적 사전분석: 배치 스캔 5분 전 (morning_scan_enabled 시 08:15)
        if morning_scan_enabled:
            prescan_hour = morning_hour
            prescan_min = max(morning_min - 5, 0)
        else:
            prescan_hour, prescan_min = scan_hour, max(scan_min - 5, 0)
            if scan_min < 5:
                prescan_hour = scan_hour - 1 if scan_hour > 0 else 23
                prescan_min = 60 + scan_min - 5

        last_scan_date = None
        last_morning_scan_date = None
        last_execute_date = None
        last_evening_scan_date = None
        last_monitor_time = None
        last_prescan_date = None
        last_expert_panel_week = None

        pending_signals_path = Path.home() / ".cache" / "ai_trader" / "pending_signals.json"

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                # 일요일 21:00 전문가 패널 (주 1회)
                if now.weekday() == 6 and now.hour == 21 and 0 <= now.minute < 15:
                    iso_week = now.isocalendar()[1]
                    if last_expert_panel_week != iso_week:
                        await self._run_expert_panel()
                        last_expert_panel_week = iso_week

                if is_kr_market_holiday(today):
                    await asyncio.sleep(60)
                    continue

                # ── catch-up 로직 ────────────────────────────────────────────
                # [아침 스캔 모드] 봇이 08:20 이후 ~ 09:01 이전에 재시작된 경우:
                #   유효한 시그널이 없으면 아침 스캔 즉시 실행
                if (morning_scan_enabled
                        and last_morning_scan_date != today
                        and (now.hour > morning_hour
                             or (now.hour == morning_hour and now.minute >= morning_min))
                        and now.hour < exec_hour):
                    # 유효한(미만료) 시그널 존재 여부 확인
                    has_valid = False
                    if pending_signals_path.exists():
                        try:
                            import json as _json
                            _sigs = _json.loads(pending_signals_path.read_text())
                            # 오늘 생성된 유효 시그널이 있는 경우에만 스캔 생략
                            # (전날 생성된 시그널은 expires_at이 오늘이어도 재스캔 필요)
                            has_valid = any(
                                datetime.fromisoformat(s.get("expires_at", "2000-01-01")) > now
                                and datetime.fromisoformat(s.get("created_at", "2000-01-01")).date() == today
                                for s in _sigs
                            )
                        except Exception:
                            has_valid = False

                    if not has_valid:
                        logger.info("[배치스케줄러] catch-up: 아침 스캔 즉시 실행")
                        try:
                            await self._run_strategic_prescan()
                            await self.batch_analyzer.run_morning_scan()
                            last_morning_scan_date = today
                            last_prescan_date = today
                        except Exception as e:
                            logger.error(f"[배치] catch-up 아침 스캔 오류: {e}")
                            last_morning_scan_date = today  # 무한 재시도 방지
                    else:
                        last_morning_scan_date = today  # 이미 유효한 시그널 있음, 재스캔 불필요

                # [공통] 시그널 있고 09:01 이후면 즉시 실행
                if (last_execute_date != today
                        and now.hour >= exec_hour
                        and now.hour < 15  # 장 마감 전까지만
                        and pending_signals_path.exists()):
                    try:
                        result = await self.batch_analyzer.execute_pending_signals()
                        last_execute_date = today
                        logger.info(f"[배치] catch-up 실행: {result}")
                    except Exception as e:
                        logger.error(f"[배치] catch-up 실행 오류: {e}")
                        last_execute_date = today  # 무한 재시도 방지

                # ── 사전분석 (prescan_hour:prescan_min) ──────────────────────
                if (now.hour == prescan_hour
                        and prescan_min <= now.minute < prescan_min + 4
                        and last_prescan_date != today):
                    await self._run_strategic_prescan()
                    last_prescan_date = today

                # ── 08:20 아침 스캔 (morning_scan_enabled=true 시) ───────────
                if (morning_scan_enabled
                        and now.hour == morning_hour
                        and morning_min <= now.minute < morning_min + 5
                        and last_morning_scan_date != today):
                    logger.info("[배치스케줄러] 아침 스캔 시작")
                    try:
                        await self.batch_analyzer.run_morning_scan()
                    except Exception as e:
                        logger.error(f"[배치스케줄러] 아침 스캔 오류: {e}")
                    last_morning_scan_date = today

                # ── 15:40 일일 스캔 (morning_scan_enabled=false 시만) ─────────
                if (not morning_scan_enabled
                        and now.hour == scan_hour
                        and scan_min <= now.minute < scan_min + 5
                        and last_scan_date != today):
                    logger.info("[배치스케줄러] 일일 스캔 시작")
                    try:
                        await self.batch_analyzer.run_daily_scan()
                    except Exception as e:
                        logger.error(f"[배치스케줄러] 일일 스캔 오류: {e}")
                    last_scan_date = today

                # ── 19:30 저녁 스캔 (morning_scan_enabled=false 시만) ─────────
                if (not morning_scan_enabled
                        and evening_scan_enabled
                        and now.hour == evening_hour
                        and evening_min <= now.minute < evening_min + 5
                        and last_evening_scan_date != today
                        and last_scan_date == today):  # 1차 스캔 완료된 날만
                    logger.info("[배치스케줄러] 저녁 스캔 시작 (넥스트장 보정)")
                    try:
                        await self.batch_analyzer.run_evening_scan()
                    except Exception as e:
                        logger.error(f"[배치스케줄러] 저녁 스캔 오류: {e}")
                    last_evening_scan_date = today

                # ── 09:01 시그널 실행 ─────────────────────────────────────────
                if (now.hour == exec_hour
                        and exec_min <= now.minute < exec_min + 4
                        and last_execute_date != today):
                    logger.info("[배치스케줄러] 시그널 실행 시작")
                    try:
                        await self.batch_analyzer.execute_pending_signals()
                    except Exception as e:
                        logger.error(f"[배치스케줄러] 시그널 실행 오류: {e}")
                    last_execute_date = today

                # 09:30~15:20 매 30분 포지션 모니터링
                if 9 <= now.hour <= 15:
                    should_monitor = False
                    if last_monitor_time is None:
                        should_monitor = (now.hour == 9 and now.minute >= 30) or now.hour >= 10
                    else:
                        elapsed = (now - last_monitor_time).total_seconds() / 60
                        should_monitor = elapsed >= monitor_interval

                    # 15:20 이후 제외
                    if now.hour == 15 and now.minute >= 20:
                        should_monitor = False

                    if should_monitor:
                        try:
                            await self.batch_analyzer.monitor_positions()
                        except Exception as e:
                            logger.error(f"[배치스케줄러] 포지션 모니터링 오류: {e}")
                        last_monitor_time = now

                await asyncio.sleep(30)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[배치스케줄러] 스케줄러 오류: {e}")

    async def _pending_cleanup_loop(self):
        """교착 pending 독립 정리 루프 (60초 주기).

        price event 없이도 장전/장중/장후 관계없이 stale pending을 주기적으로 해제.
        CLOSED 세션에는 실행하지 않음 (불필요한 KIS API 호출 방지).
        """
        await asyncio.sleep(30)  # 초기 대기 (봇 초기화 완료 후 시작)
        while self.running:
            try:
                session = self._get_current_session()
                if session != MarketSession.CLOSED:
                    if hasattr(self, '_cleanup_stale_pending'):
                        await self._cleanup_stale_pending()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[pending 정리] 오류: {e}")
            await asyncio.sleep(60)

    async def _run_log_cleanup(self):
        """
        로그/캐시 정리 스케줄러

        매일 00:05에 오래된 로그 디렉터리, 로그 파일, 캐시 JSON 정리
        """
        try:
            while self.running:
                now = datetime.now()

                # 매일 00:05 ~ 00:10 에 실행
                if now.hour == 0 and 5 <= now.minute < 10:
                    try:
                        from pathlib import Path
                        log_base = Path(__file__).parent.parent / "logs"
                        cleanup_old_logs(str(log_base), max_days=7)
                        cleanup_old_cache(max_days=7)
                        logger.info("[스케줄러] 로그/캐시 정리 완료")
                    except Exception as e:
                        logger.error(f"[스케줄러] 로그 정리 오류: {e}")

                    # 같은 날 다시 실행 방지 (10분 대기)
                    await asyncio.sleep(600)
                else:
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"로그 정리 스케줄러 오류: {e}")

    async def _run_health_monitor(self):
        """헬스 모니터링 루프"""
        try:
            if self.health_monitor:
                await self.health_monitor.run_loop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[HealthMonitor] 루프 종료: {e}")
