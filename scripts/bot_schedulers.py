"""
백그라운드 스케줄러 및 주기적 작업 Mixin

run_trader.py의 TradingBot에서 상속하여 사용.
레포트, 진화, 테마 탐지, 스크리닝, 체결 확인, 포트폴리오 동기화 등
백그라운드 루프 메서드를 분리한 모듈.
"""

import asyncio
import aiohttp
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Optional

from loguru import logger

from src.core.engine import is_kr_market_holiday
from src.core.event import ThemeEvent, NewsEvent, FillEvent
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
        config_syms = set(self.config.get("watch_symbols", []))
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

        last_morning_report: Optional[date] = None
        last_evening_report: Optional[date] = None
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
                                from src.core.engine import _kr_market_holidays
                                _kr_market_holidays.update(h)
                                logger.info(f"[휴장일] 익월({next_month}) 휴장일 {len(h)}일 추가 로드")
                            last_holiday_refresh_month = next_month
                        except Exception as e:
                            logger.warning(f"[휴장일] 익월 휴장일 갱신 실패: {e}")

                # 자정: 일일 통계 + 전략 상태 초기화 (공휴일 포함 매일 실행)
                if last_daily_reset != today:
                    try:
                        self.engine.reset_daily_stats()
                        if self.risk_manager:
                            self.risk_manager.reset_daily_stats()

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
                        if self.engine.risk_manager:
                            self.engine.risk_manager._pending_orders.clear()
                            self.engine.risk_manager._pending_quantities.clear()
                            self.engine.risk_manager._pending_timestamps.clear()
                            self.engine.risk_manager._pending_sides.clear()

                        # 거래 로거 일일 기록 플러시 및 초기화
                        trading_logger.flush()
                        trading_logger._daily_records.clear()

                        last_daily_reset = today
                        logger.info("[스케줄러] 일일 통계 + 전략 상태 + pending 주문 + 거래로그 초기화 완료")
                    except Exception as e:
                        logger.error(f"[스케줄러] 일일 초기화 실패: {e}")

                # 공휴일(주말 포함)이면 레포트 스킵
                if is_kr_market_holiday(today):
                    await asyncio.sleep(60)
                    continue

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
                        except Exception as e:
                            logger.error(f"[레포트] 오후 레포트 발송 실패: {e}")

                # 1분마다 체크
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"레포트 스케줄러 오류: {e}")

    async def _run_evolution_scheduler(self):
        """
        자가 진화 스케줄러

        - 넥스트장 마감 후: 일일 진화 실행
          1. 거래 저널에서 데이터 분석
          2. LLM으로 전략 개선안 도출
          3. 파라미터 자동 조정
          4. 효과 평가 및 롤백
        """
        last_evolution_date: Optional[date] = None

        # config에서 진화 실행 시간 로드
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

                # 넥스트장 마감 후 일일 진화 실행 (설정 시간 ~ +15분)
                if now.hour == evo_hour and evo_min <= now.minute < evo_min + 15:
                    if last_evolution_date != today:
                        logger.info("[진화] 일일 자가 진화 시작...")

                        try:
                            # 1. 복기 및 진화 실행
                            evolution_cfg = self.config.get("evolution") or {}
                            analysis_days = evolution_cfg.get("analysis_days", 7)
                            min_trades = evolution_cfg.get("min_trades_for_evolution", 5)

                            # 최소 거래 수 체크
                            recent_trades = self.trade_journal.get_recent_trades(days=analysis_days)

                            if len(recent_trades) >= min_trades:
                                # 진화 실행
                                result = await self.strategy_evolver.evolve(days=analysis_days)

                                if result:
                                    # 진화 결과 로깅
                                    logger.info(
                                        f"[진화] 완료 - 평가={result.overall_assessment}, "
                                        f"인사이트 {len(result.key_insights)}개, "
                                        f"파라미터 조정 {len(result.parameter_adjustments)}개"
                                    )

                                    # 핵심 인사이트 로그
                                    for insight in result.key_insights[:3]:
                                        logger.info(f"  [인사이트] {insight}")

                                    # 파라미터 변경 로그
                                    for adj in result.parameter_adjustments:
                                        logger.info(
                                            f"  [파라미터] {adj.parameter}: "
                                            f"{adj.current_value} -> {adj.suggested_value} "
                                            f"(신뢰도: {adj.confidence:.0%})"
                                        )

                                    # 텔레그램 알림 (선택적)
                                    if evolution_cfg.get("send_telegram", True):
                                        await self._send_evolution_report(result)

                                    # 거래 로그에 기록 (복기용)
                                    trading_logger.log_evolution(
                                        assessment=result.overall_assessment,
                                        confidence=result.confidence_score,
                                        insights=result.key_insights,
                                        parameter_changes=[
                                            {
                                                "parameter": p.parameter,
                                                "from": p.current_value,
                                                "to": p.suggested_value,
                                                "confidence": p.confidence,
                                            }
                                            for p in result.parameter_adjustments
                                        ],
                                    )
                                else:
                                    logger.info("[진화] 진화 결과 없음 (변경 불필요)")
                            else:
                                logger.info(
                                    f"[진화] 거래 부족으로 스킵 "
                                    f"({len(recent_trades)}/{min_trades}건)"
                                )

                            last_evolution_date = today

                        except Exception as e:
                            logger.error(f"[진화] 실행 오류: {e}")
                            import traceback
                            await self._send_error_alert(
                                "ERROR",
                                "자가 진화 실행 오류",
                                traceback.format_exc()
                            )

                # 매 시간 정각에 진화 효과 평가 (적용된 변경이 있는 경우)
                if now.minute < 15 and 9 <= now.hour <= 15:
                    try:
                        # 진화 상태 확인 및 효과 평가
                        state = self.strategy_evolver.get_evolution_state()

                        if state and state.active_changes:
                            evaluation = await self.strategy_evolver.evaluate_changes()

                            if evaluation:
                                logger.info(
                                    f"[진화 평가] 활성 변경 {len(state.active_changes)}개, "
                                    f"효과: {evaluation.get('effectiveness', 'unknown')}"
                                )

                                # 효과 없으면 롤백 고려
                                if evaluation.get('should_rollback', False):
                                    logger.warning("[진화] 효과 없음 - 롤백 실행")
                                    await self.strategy_evolver.rollback_last_change()

                    except Exception as e:
                        logger.error(f"[진화 평가] 오류: {e}")

                # 1분마다 체크
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"진화 스케줄러 오류: {e}")

    async def _send_evolution_report(self, result):
        """진화 결과 텔레그램 알림"""
        try:
            emoji_map = {"good": "✅", "fair": "⚠️", "poor": "❌", "no_data": "📊"}
            emoji = emoji_map.get(result.overall_assessment, "📊")

            text = f"""
{emoji} <b>AI Trader v2 - 일일 진화 리포트</b>

<b>분석 기간:</b> 최근 {result.period_days}일
<b>전체 평가:</b> {result.overall_assessment.upper()}
<b>신뢰도:</b> {result.confidence_score:.0%}

<b>핵심 인사이트:</b>
"""
            for i, insight in enumerate(result.key_insights[:5], 1):
                text += f"{i}. {insight}\n"

            if result.parameter_adjustments:
                text += "\n<b>파라미터 조정:</b>\n"
                for adj in result.parameter_adjustments[:3]:
                    text += (
                        f"- {adj.parameter}: {adj.current_value} -> {adj.suggested_value} "
                        f"({adj.confidence:.0%})\n"
                    )

            if result.next_week_outlook:
                text += f"\n<b>전망:</b> {result.next_week_outlook[:200]}"

            await send_alert(text)

        except Exception as e:
            logger.error(f"진화 리포트 전송 실패: {e}")

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
                                f"KOSPI200={stats.get('kospi200', 0)}, "
                                f"KOSDAQ150={stats.get('kosdaq150', 0)}"
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

                            # 테마 관련 종목 WebSocket 구독 추가
                            if self.ws_feed and theme.related_stocks:
                                async with self._watch_symbols_lock:
                                    new_symbols = [s for s in theme.related_stocks
                                                 if s not in self._watch_symbols]
                                    if new_symbols:
                                        await self.ws_feed.subscribe(new_symbols[:10])
                                        self._watch_symbols.extend(new_symbols[:10])
                                        logger.info(f"[테마 탐지] 신규 종목 구독: {new_symbols[:10]}")

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

                                # WebSocket 구독에 자동 추가
                                if self.ws_feed:
                                    async with self._watch_symbols_lock:
                                        if symbol not in self._watch_symbols:
                                            await self.ws_feed.subscribe([symbol])
                                            self._watch_symbols.append(symbol)
                                            logger.info(
                                                f"[뉴스 임팩트] {symbol} 구독 추가 "
                                                f"(impact={impact}, {direction})"
                                            )

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

                    # 신규 종목 WebSocket 구독 (점수와 함께)
                    if self.ws_feed:
                        # 전체 점수 업데이트
                        self.ws_feed.set_symbol_scores(scores)

                        if new_symbols:
                            # 신규 종목 구독 (롤링 방식으로 자동 관리)
                            await self.ws_feed.subscribe(new_symbols, scores)
                            stats = self.ws_feed.get_subscription_stats()
                            logger.info(
                                f"[스크리닝] 신규 {len(new_symbols)}개 추가 → "
                                f"총 감시={stats['total_watch']}, 구독={stats['subscribed_count']}, "
                                f"롤링대기={stats['rolling_queue_size']}"
                            )

                            # 감시 종목 변경 로그
                            trading_logger.log_watchlist_update(
                                added=new_symbols,
                                removed=[],
                                total=stats['total_watch'],
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

                except Exception as e:
                    logger.warning(f"스크리닝 오류: {e}")

                # 다음 스캔까지 대기
                await asyncio.sleep(self._screening_interval)

        except asyncio.CancelledError:
            pass

    async def _run_fill_check(self):
        """체결 확인 루프 (적응형 폴링: 미체결 유무에 따라 2초/5초)"""
        check_interval = 5  # 초 (기본값)

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
                    if hasattr(self, '_fill_check_errors') and self._fill_check_errors > 0:
                        self._fill_check_errors = 0

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"체결 확인 네트워크 오류: {e}")
                    if not hasattr(self, '_fill_check_errors'):
                        self._fill_check_errors = 0
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
                    if not hasattr(self, '_fill_check_errors'):
                        self._fill_check_errors = 0
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

            # 2. lock 내에서 포트폴리오 수정 (다른 태스크와 동시 접근 방지)
            async with self._portfolio_lock:
                portfolio = self.engine.portfolio
                kis_symbols = set(kis_positions.keys()) if kis_positions else set()
                bot_symbols = set(portfolio.positions.keys())

                # API 빈 결과 방어: 봇에 포지션이 있는데 KIS가 0개 반환하면 1회 재시도
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

                # 누락 포지션 추가 (KIS에 있고 봇에 없는 종목)
                new_symbols = kis_symbols - bot_symbols
                for symbol in new_symbols:
                    pos = kis_positions[symbol]
                    portfolio.positions[symbol] = pos
                    logger.info(
                        f"[동기화] 포지션 추가: {symbol} {pos.name} "
                        f"({pos.quantity}주 @ {pos.avg_price:,.0f}원)"
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

            changes = len(ghost_symbols) + len(new_symbols)
            if changes > 0:
                logger.info(
                    f"[동기화] 완료: 제거={len(ghost_symbols)}, "
                    f"추가={len(new_symbols)}, "
                    f"보유={len(portfolio.positions)}종목"
                )
                trading_logger.log_portfolio_sync(
                    ghost_removed=len(ghost_symbols),
                    new_added=len(new_symbols),
                    total_positions=len(portfolio.positions),
                    cash=float(portfolio.cash),
                    total_equity=float(portfolio.total_equity),
                )
            else:
                logger.debug(
                    f"[동기화] 확인 완료: 보유={len(portfolio.positions)}종목, 변경 없음"
                )

        except Exception as e:
            logger.error(f"포트폴리오 동기화 오류: {e}")

    async def _run_code_evolution_scheduler(self):
        """
        코드 자동 진화 스케줄러

        - 매일 또는 주 1회 지정 시간에 실행 (schedule_daily 설정)
        - 또는 연속 롤백 3회 시 트리거
        - auto_merge=true 시 자동 머지 + 봇 재시작
        """
        from src.core.evolution.code_evolver import get_code_evolver

        code_evo_cfg = self.config.get("code_evolution") or {}
        if not code_evo_cfg.get("enabled", False):
            logger.info("[코드진화] 비활성화됨 (code_evolution.enabled=false)")
            return

        schedule_daily = code_evo_cfg.get("schedule_daily", False)  # 매일 실행 여부
        schedule_day = code_evo_cfg.get("schedule_day", 5)  # 0=월, 5=토 (주간 실행 시)
        schedule_hour = code_evo_cfg.get("schedule_hour", 10)
        auto_merge = code_evo_cfg.get("auto_merge", False)  # 자동 머지 여부
        last_run_date = None

        code_evolver = get_code_evolver()

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                # 스케줄 조건: 매일 or 특정 요일
                if schedule_daily:
                    # 매일 지정 시간 (±15분)
                    scheduled_run = (
                        now.hour == schedule_hour
                        and 0 <= now.minute < 15
                        and last_run_date != today
                    )
                else:
                    # 주 1회 특정 요일 지정 시간 (±15분)
                    scheduled_run = (
                        now.weekday() == schedule_day
                        and now.hour == schedule_hour
                        and 0 <= now.minute < 15
                        and last_run_date != today
                    )

                # 연속 롤백 트리거
                rollback_trigger = code_evolver.should_trigger_by_rollbacks

                if scheduled_run or rollback_trigger:
                    trigger = "scheduled" if scheduled_run else "rollback_threshold"
                    logger.info(f"[코드진화] 스케줄러 트리거: {trigger}")

                    try:
                        result = await code_evolver.run_evolution(
                            trigger_reason=trigger,
                            auto_merge=auto_merge,
                        )

                        if result["success"]:
                            logger.info(f"[코드진화] 성공: {result['pr_url']}")

                            # 텔레그램 알림
                            try:
                                msg = (
                                    f"<b>[코드진화]</b> PR 생성\n"
                                    f"사유: {trigger}\n"
                                    f"변경: {result['changed_files']}개 파일\n"
                                    f"PR: {result['pr_url']}"
                                )
                                if result.get("auto_merged"):
                                    msg += "\n✅ 자동 머지 완료"
                                await send_alert(msg)
                            except Exception:
                                pass

                            # 자동 머지 성공 시 봇 재시작
                            if result.get("auto_merged"):
                                logger.info("[코드진화] 자동 머지 완료 → 5초 후 봇 재시작")
                                await send_alert(
                                    "<b>[코드진화]</b> 자동 머지 완료\n"
                                    "5초 후 봇 재시작..."
                                )
                                await asyncio.sleep(5)
                                # 봇 재시작 (main 복귀 후 프로세스 종료 → systemd/supervisor가 재시작)
                                logger.warning("[코드진화] 봇 재시작 중...")
                                os._exit(0)  # 즉시 종료 (systemd/cron이 재시작)

                        else:
                            logger.warning(f"[코드진화] 실패: {result['message']}")
                            # 실패 텔레그램 알림
                            try:
                                await send_alert(
                                    f"<b>[코드진화]</b> 실패\n"
                                    f"사유: {result['message'][:200]}"
                                )
                            except Exception:
                                pass

                        last_run_date = today

                    except Exception as e:
                        logger.error(f"[코드진화] 실행 오류: {e}")
                        last_run_date = today
                        try:
                            await send_alert(
                                f"<b>[코드진화]</b> 실행 오류\n"
                                f"{str(e)[:200]}"
                            )
                        except Exception:
                            pass

                await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[코드진화] 스케줄러 오류: {e}")

    async def _run_portfolio_sync(self):
        """주기적 포트폴리오 동기화 루프"""
        await asyncio.sleep(30)  # 시작 후 30초 대기
        while self.running:
            try:
                await self._sync_portfolio()
            except Exception as e:
                logger.error(f"동기화 루프 오류: {e}")
            await asyncio.sleep(120)  # 2분마다 동기화 (KIS API 응답 지연 대응)

    async def _run_batch_scheduler(self):
        """
        스윙 모멘텀 배치 스케줄러

        - 15:40 일일 스캔 (장 마감 후)
        - 09:01 시그널 실행 (장 시작 후)
        - 09:30~15:20 매 30분 포지션 모니터링
        """
        if not hasattr(self, 'batch_analyzer') or not self.batch_analyzer:
            logger.info("[배치스케줄러] batch_analyzer 없음, 스킵")
            return

        # config에서 스케줄 시간 로드
        batch_cfg = self.config.get("batch") or {}
        scan_time_str = batch_cfg.get("daily_scan_time", "15:40")
        execute_time_str = batch_cfg.get("execute_time", "09:01")
        monitor_interval = batch_cfg.get("position_update_interval", 30)  # 분

        scan_hour, scan_min = (int(x) for x in scan_time_str.split(":"))
        exec_hour, exec_min = (int(x) for x in execute_time_str.split(":"))

        last_scan_date = None
        last_execute_date = None
        last_monitor_time = None

        try:
            while self.running:
                now = datetime.now()
                today = now.date()

                if is_kr_market_holiday(today):
                    await asyncio.sleep(60)
                    continue

                # 15:40 일일 스캔
                if (now.hour == scan_hour
                        and scan_min <= now.minute < scan_min + 5
                        and last_scan_date != today):
                    logger.info("[배치스케줄러] 일일 스캔 시작")
                    try:
                        await self.batch_analyzer.run_daily_scan()
                    except Exception as e:
                        logger.error(f"[배치스케줄러] 일일 스캔 오류: {e}")
                    last_scan_date = today

                # 09:01 시그널 실행
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
