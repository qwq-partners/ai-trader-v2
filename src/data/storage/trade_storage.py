"""
AI Trading Bot v2 - PostgreSQL 거래 저장소

TradeJournal 인터페이스 100% 호환 + DB 영속화 + trade_events 이벤트 로그.
DB 연결 실패 시 JSON 전용 모드로 자동 폴백.
"""

import asyncio
import json
import os
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

import asyncpg
from loguru import logger

from src.core.evolution.trade_journal import TradeJournal, TradeRecord
from src.utils.fee_calculator import FeeConfig


# ── SQL 스키마 ──────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id              VARCHAR(80) PRIMARY KEY,
    symbol          VARCHAR(10)  NOT NULL,
    name            VARCHAR(100) NOT NULL DEFAULT '',
    entry_time      TIMESTAMP    NOT NULL,
    entry_price     NUMERIC(12,2) NOT NULL,
    entry_quantity  INTEGER      NOT NULL,
    entry_reason    TEXT         DEFAULT '',
    entry_strategy  VARCHAR(50)  DEFAULT '',
    entry_signal_score NUMERIC(6,2) DEFAULT 0,
    exit_time       TIMESTAMP    NULL,
    exit_price      NUMERIC(12,2) DEFAULT 0,
    exit_quantity   INTEGER      DEFAULT 0,
    exit_reason     TEXT         DEFAULT '',
    exit_type       VARCHAR(30)  DEFAULT '',
    pnl             NUMERIC(14,2) DEFAULT 0,
    pnl_pct         NUMERIC(8,4)  DEFAULT 0,
    holding_minutes INTEGER       DEFAULT 0,
    market_context       JSONB DEFAULT '{}',
    indicators_at_entry  JSONB DEFAULT '{}',
    indicators_at_exit   JSONB DEFAULT '{}',
    theme_info           JSONB DEFAULT '{}',
    review_notes            TEXT DEFAULT '',
    lesson_learned          TEXT DEFAULT '',
    improvement_suggestion  TEXT DEFAULT '',
    kis_order_no VARCHAR(20) NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(entry_strategy);
CREATE INDEX IF NOT EXISTS idx_trades_entry_date ON trades((entry_time::date));
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(exit_time) WHERE exit_time IS NULL;

CREATE TABLE IF NOT EXISTS trade_events (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        VARCHAR(80) NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    symbol          VARCHAR(10) NOT NULL,
    name            VARCHAR(100) DEFAULT '',
    event_type      VARCHAR(10) NOT NULL,
    event_time      TIMESTAMP   NOT NULL,
    price           NUMERIC(12,2) NOT NULL,
    quantity        INTEGER     NOT NULL,
    exit_type       VARCHAR(30) NULL,
    exit_reason     TEXT        NULL,
    pnl             NUMERIC(14,2) NULL,
    pnl_pct         NUMERIC(8,4)  NULL,
    strategy        VARCHAR(50) DEFAULT '',
    signal_score    NUMERIC(6,2) DEFAULT 0,
    kis_order_no    VARCHAR(20) NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'holding',
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_te_event_time ON trade_events(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_te_trade_id ON trade_events(trade_id);
CREATE INDEX IF NOT EXISTS idx_te_type ON trade_events(event_type);
CREATE INDEX IF NOT EXISTS idx_te_date ON trade_events((event_time::date));
"""


class TradeStorage:
    """
    PostgreSQL 기반 거래 저장소.

    - TradeJournal과 동일한 동기 인터페이스 제공 (인메모리 캐시)
    - DB 쓰기는 asyncio.Queue → 백그라운드 writer 코루틴으로 비동기 처리
    - JSON 백업은 내부 TradeJournal 인스턴스에 위임
    """

    def __init__(self, db_url: str = None):
        self.db_url = db_url or os.getenv("DATABASE_URL", "")
        self.pool: Optional[asyncpg.Pool] = None
        self._db_available = False

        # JSON 백업 전담
        self._journal = TradeJournal()

        # 인메모리 캐시 (TradeJournal에서 가져옴)
        self._trades = self._journal._trades
        self._today_trades = self._journal._today_trades

        # DB 비동기 쓰기 큐
        self._write_queue: Optional[asyncio.Queue] = None
        self._writer_task: Optional[asyncio.Task] = None

    # ── 라이프사이클 ──────────────────────────────────────

    async def connect(self):
        """DB 연결 + 스키마 생성 + writer 시작"""
        if not self.db_url:
            logger.warning("[TradeStorage] DATABASE_URL 미설정, JSON 전용 모드")
            return

        try:
            self.pool = await asyncpg.create_pool(
                self.db_url, min_size=1, max_size=5, command_timeout=30
            )
            await self._ensure_tables()
            self._db_available = True

            # writer 코루틴 시작
            self._write_queue = asyncio.Queue()
            self._writer_task = asyncio.create_task(self._db_writer())

            logger.info("[TradeStorage] DB 연결 완료, 듀얼 라이트 모드")
        except Exception as e:
            logger.error(f"[TradeStorage] DB 연결 실패, JSON 폴백: {e}")
            self._db_available = False

    async def disconnect(self):
        """큐 drain + DB 연결 종료"""
        # writer 중지
        if self._writer_task and not self._writer_task.done():
            if self._write_queue:
                await self._write_queue.put(None)  # sentinel
                try:
                    await asyncio.wait_for(self._writer_task, timeout=10)
                except asyncio.TimeoutError:
                    self._writer_task.cancel()
                    logger.warning("[TradeStorage] writer 타임아웃, 강제 종료")

        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("[TradeStorage] DB 연결 종료")

    async def _ensure_tables(self):
        """테이블 + 인덱스 생성"""
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info("[TradeStorage] 테이블 확인/생성 완료")

    @staticmethod
    def _refine_exit_type(exit_type: str, exit_reason: str) -> str:
        """exit_reason에 구체적 정보가 있으면 exit_type 세분화"""
        if not exit_reason:
            return exit_type
        r = exit_reason.lower()
        # take_profit → first/second/third 세분화
        if exit_type in ("take_profit", "unknown", ""):
            if "1차 익절" in r or "1차익절" in r:
                return "first_take_profit"
            if "2차 익절" in r or "2차익절" in r:
                return "second_take_profit"
            if "3차 익절" in r or "3차익절" in r:
                return "third_take_profit"
        # reason에서 추론 가능한데 exit_type이 unknown인 경우
        if exit_type == "unknown":
            if "손절" in r:
                return "stop_loss"
            if "트레일링" in r:
                return "trailing"
            if "본전" in r:
                return "breakeven"
            if "익절" in r:
                return "take_profit"
        return exit_type

    # ── DB 비동기 Writer ──────────────────────────────────

    async def _db_writer(self):
        """큐에서 (sql, params) 꺼내 순차 실행"""
        while True:
            item = await self._write_queue.get()
            if item is None:  # shutdown sentinel
                self._write_queue.task_done()
                break

            sql, params, retries_left = item
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(sql, *params)
            except Exception as e:
                if retries_left > 0:
                    await asyncio.sleep(1 * (4 - retries_left))  # 백오프: 1초, 2초, 3초
                    await self._write_queue.put((sql, params, retries_left - 1))
                    logger.warning(f"[TradeStorage] DB 쓰기 재시도 ({retries_left}): {e}")
                else:
                    logger.error(f"[TradeStorage] DB 쓰기 최종 실패 (JSON 백업 유지): {e}")
            finally:
                self._write_queue.task_done()

    def _enqueue(self, sql: str, params: tuple):
        """DB 쓰기 큐에 추가 (동기 호출 가능)"""
        if not self._db_available or not self._write_queue:
            return
        try:
            self._write_queue.put_nowait((sql, params, 3))
        except Exception as e:
            logger.warning(f"[TradeStorage] 큐 추가 실패: {e}")

    # ── TradeJournal 호환 인터페이스 (동기) ────────────────

    def record_entry(
        self,
        trade_id: str,
        symbol: str,
        name: str,
        entry_price: float,
        entry_quantity: int,
        entry_reason: str,
        entry_strategy: str,
        signal_score: float = 0,
        indicators: Dict[str, float] = None,
        market_context: Dict[str, Any] = None,
        theme_info: Dict[str, Any] = None,
    ) -> TradeRecord:
        """진입 기록: 캐시 + JSON + DB큐"""
        # 1) 캐시 + JSON (동기)
        trade = self._journal.record_entry(
            trade_id=trade_id,
            symbol=symbol,
            name=name,
            entry_price=entry_price,
            entry_quantity=entry_quantity,
            entry_reason=entry_reason,
            entry_strategy=entry_strategy,
            signal_score=signal_score,
            indicators=indicators,
            market_context=market_context,
            theme_info=theme_info,
        )

        # 2) DB 큐 — trades INSERT
        self._enqueue(
            """INSERT INTO trades
               (id, symbol, name, entry_time, entry_price, entry_quantity,
                entry_reason, entry_strategy, entry_signal_score,
                market_context, indicators_at_entry, theme_info, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
               ON CONFLICT (id) DO NOTHING""",
            (
                trade.id, trade.symbol, trade.name,
                trade.entry_time, float(trade.entry_price), trade.entry_quantity,
                trade.entry_reason, trade.entry_strategy, float(trade.entry_signal_score),
                json.dumps(trade.market_context, default=str, ensure_ascii=False),
                json.dumps(trade.indicators_at_entry, default=str, ensure_ascii=False),
                json.dumps(trade.theme_info, default=str, ensure_ascii=False),
                trade.created_at, trade.updated_at,
            ),
        )

        # 3) DB 큐 — trade_events BUY INSERT
        self._enqueue(
            """INSERT INTO trade_events
               (trade_id, symbol, name, event_type, event_time, price, quantity,
                strategy, signal_score, status)
               VALUES ($1,$2,$3,'BUY',$4,$5,$6,$7,$8,'holding')""",
            (
                trade.id, trade.symbol, trade.name,
                trade.entry_time, float(trade.entry_price), trade.entry_quantity,
                trade.entry_strategy, float(trade.entry_signal_score),
            ),
        )

        return trade

    def record_exit(
        self,
        trade_id: str,
        exit_price: float,
        exit_quantity: int,
        exit_reason: str,
        exit_type: str,
        indicators: Dict[str, float] = None,
        exit_time: datetime = None,
        avg_entry_price: float = None,
    ) -> Optional[TradeRecord]:
        """청산 기록: 캐시 + JSON + DB큐"""
        # exit_type 세분화: reason에 구체적 정보가 있으면 재분류
        exit_type = self._refine_exit_type(exit_type, exit_reason)

        # 누적 PnL 캡처 (이번 매도분 PnL 계산용)
        prev_pnl = 0.0
        prev_trade = self._journal.get_trade(trade_id)
        if prev_trade:
            prev_pnl = float(prev_trade.pnl)

        # 1) 캐시 + JSON
        trade = self._journal.record_exit(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_quantity=exit_quantity,
            exit_reason=exit_reason,
            exit_type=exit_type,
            indicators=indicators,
            exit_time=exit_time,
            avg_entry_price=avg_entry_price,
        )
        if not trade:
            return None

        # 이번 매도분 PnL (누적 - 이전)
        this_sell_pnl = float(trade.pnl) - prev_pnl
        entry_price_for_pct = avg_entry_price or float(trade.entry_price)
        invested_this = entry_price_for_pct * exit_quantity
        this_sell_pnl_pct = (this_sell_pnl / invested_this * 100) if invested_this > 0 else 0.0

        # 상태 결정
        total_exited = trade.exit_quantity or 0
        is_fully_closed = total_exited >= trade.entry_quantity
        status = exit_type if is_fully_closed else "partial"

        # 2) DB 큐 — trades UPDATE (누적 PnL)
        self._enqueue(
            """UPDATE trades SET
               exit_time=$1, exit_price=$2, exit_quantity=$3,
               exit_reason=$4, exit_type=$5, pnl=$6, pnl_pct=$7,
               holding_minutes=$8, indicators_at_exit=$9, updated_at=$10
               WHERE id=$11""",
            (
                trade.exit_time, float(trade.exit_price), trade.exit_quantity,
                trade.exit_reason, trade.exit_type,
                float(trade.pnl), float(trade.pnl_pct),
                trade.holding_minutes,
                json.dumps(trade.indicators_at_exit, default=str, ensure_ascii=False),
                trade.updated_at, trade.id,
            ),
        )

        # 3) DB 큐 — trade_events SELL INSERT (이번 매도분 PnL)
        self._enqueue(
            """INSERT INTO trade_events
               (trade_id, symbol, name, event_type, event_time, price, quantity,
                exit_type, exit_reason, pnl, pnl_pct, strategy, signal_score, status)
               VALUES ($1,$2,$3,'SELL',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            (
                trade.id, trade.symbol, trade.name,
                trade.exit_time, float(exit_price), exit_quantity,
                exit_type, exit_reason,
                float(this_sell_pnl), float(this_sell_pnl_pct),
                trade.entry_strategy, float(trade.entry_signal_score),
                status,
            ),
        )

        # 4) 미청산 BUY 이벤트 상태 업데이트
        if is_fully_closed:
            self._enqueue(
                """UPDATE trade_events SET status=$1
                   WHERE trade_id=$2 AND event_type='BUY'""",
                (status, trade.id),
            )

        return trade

    def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        return self._journal.get_trade(trade_id)

    def get_today_trades(self) -> List[TradeRecord]:
        return self._journal.get_today_trades()

    def get_trades_by_date(self, trade_date: date) -> List[TradeRecord]:
        return self._journal.get_trades_by_date(trade_date)

    def get_trades_by_strategy(self, strategy: str, days: int = 30) -> List[TradeRecord]:
        return self._journal.get_trades_by_strategy(strategy, days)

    def get_closed_trades(self, days: int = 30) -> List[TradeRecord]:
        return self._journal.get_closed_trades(days)

    def get_open_trades(self) -> List[TradeRecord]:
        return self._journal.get_open_trades()

    def get_recent_trades(self, days: int = 7) -> List[TradeRecord]:
        return self._journal.get_recent_trades(days)

    def get_statistics(self, days: int = 30) -> Dict[str, Any]:
        return self._journal.get_statistics(days)

    def update_review(
        self,
        trade_id: str,
        review_notes: str = "",
        lesson_learned: str = "",
        improvement_suggestion: str = "",
    ):
        self._journal.update_review(
            trade_id, review_notes, lesson_learned, improvement_suggestion
        )
        self._enqueue(
            """UPDATE trades SET review_notes=$1, lesson_learned=$2,
               improvement_suggestion=$3, updated_at=$4 WHERE id=$5""",
            (review_notes, lesson_learned, improvement_suggestion,
             datetime.now(), trade_id),
        )

    # ── 새 메서드: trade_events DB 쿼리 ──────────────────

    async def get_trade_events(
        self,
        target_date: date = None,
        event_type: str = "all",
        limit: int = 200,
    ) -> List[Dict]:
        """
        trade_events 테이블에서 이벤트 로그 조회.

        DB 미연결 시 캐시에서 이벤트 구성.
        """
        if not self._db_available:
            return self._get_events_from_cache(target_date, event_type)

        target_date = target_date or date.today()
        try:
            sql = """
                SELECT te.*, t.entry_price, t.entry_quantity
                FROM trade_events te
                JOIN trades t ON te.trade_id = t.id
                WHERE te.event_time::date = $1
                  -- KIS_SYNC BUY 중복 제거: 봇 BUY가 있으면 KIS_SYNC BUY 숨김
                  AND NOT (
                    te.trade_id LIKE 'KIS_SYNC_%%'
                    AND EXISTS (
                      SELECT 1 FROM trade_events te2
                      WHERE te2.symbol = te.symbol
                        AND te2.event_type = te.event_type
                        AND te2.event_time::date = te.event_time::date
                        AND te2.trade_id NOT LIKE 'KIS_SYNC_%%'
                    )
                  )
                  -- kis_sync SELL 중복 제거: 봇 SELL이 있으면 kis_sync SELL 숨김
                  AND NOT (
                    te.event_type = 'SELL' AND te.exit_type = 'kis_sync'
                    AND EXISTS (
                      SELECT 1 FROM trade_events te3
                      WHERE te3.trade_id = te.trade_id
                        AND te3.event_type = 'SELL'
                        AND te3.exit_type IS DISTINCT FROM 'kis_sync'
                    )
                  )
            """
            params = [target_date]

            if event_type and event_type != "all":
                sql += " AND te.event_type = $2"
                params.append(event_type.upper())

            sql += " ORDER BY te.event_time DESC LIMIT $" + str(len(params) + 1)
            params.append(limit)

            async with self.pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)

            return [self._row_to_event_dict(row) for row in rows]
        except Exception as e:
            logger.warning(f"[TradeStorage] trade_events 쿼리 실패, 캐시 폴백: {e}")
            return self._get_events_from_cache(target_date, event_type)

    def _row_to_event_dict(self, row) -> Dict:
        """asyncpg Row → dict 변환"""
        d = dict(row)
        # Decimal → float, datetime → isoformat
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = float(v)
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
        return d

    def _get_events_from_cache(
        self, target_date: date = None, event_type: str = "all"
    ) -> List[Dict]:
        """DB 미사용 시 캐시 기반 이벤트 목록 구성"""
        target_date = target_date or date.today()
        events = []

        for trade in self._trades.values():
            if not trade.entry_time:
                continue

            # 진입 이벤트
            if trade.entry_time.date() == target_date:
                if event_type in ("all", "buy"):
                    exit_qty = trade.exit_quantity or 0
                    is_closed = exit_qty >= trade.entry_quantity
                    status = trade.exit_type if is_closed else "holding"
                    events.append({
                        "trade_id": trade.id,
                        "symbol": trade.symbol,
                        "name": trade.name,
                        "event_type": "BUY",
                        "event_time": trade.entry_time.isoformat(),
                        "price": float(trade.entry_price),
                        "quantity": trade.entry_quantity,
                        "strategy": trade.entry_strategy,
                        "signal_score": float(trade.entry_signal_score),
                        "status": status,
                        "entry_price": float(trade.entry_price),
                        "entry_quantity": trade.entry_quantity,
                    })

            # 청산 이벤트
            if trade.exit_time and trade.exit_time.date() == target_date:
                if event_type in ("all", "sell"):
                    events.append({
                        "trade_id": trade.id,
                        "symbol": trade.symbol,
                        "name": trade.name,
                        "event_type": "SELL",
                        "event_time": trade.exit_time.isoformat(),
                        "price": float(trade.exit_price),
                        "quantity": trade.exit_quantity or 0,
                        "exit_type": trade.exit_type,
                        "exit_reason": trade.exit_reason,
                        "pnl": float(trade.pnl),
                        "pnl_pct": float(trade.pnl_pct),
                        "strategy": trade.entry_strategy,
                        "signal_score": float(trade.entry_signal_score),
                        "status": trade.exit_type or "closed",
                        "entry_price": float(trade.entry_price),
                        "entry_quantity": trade.entry_quantity,
                    })

        # 역시간순 정렬
        events.sort(key=lambda e: e["event_time"], reverse=True)
        return events

    # ── KIS 동기화 ────────────────────────────────────────

    # 수수료/세금 상수 — FeeCalculator 기준값 사용 (fee_calculator.py 단일 소스)
    _fee_config = FeeConfig()
    BUY_FEE_RATE = float(_fee_config.buy_commission_rate)    # 매수 수수료 0.0140527%
    SELL_FEE_RATE = float(_fee_config.sell_commission_rate)   # 매도 수수료 0.0130527%
    SELL_TAX_RATE = float(_fee_config.sell_tax_rate)          # 증권거래세 0.20%

    @staticmethod
    def _parse_kis_time(ord_tmd: str, base_date: date) -> Optional[datetime]:
        """KIS ord_tmd (HHMMSS) → datetime 변환"""
        if not ord_tmd or len(ord_tmd) < 6:
            return None
        try:
            h, m, s = int(ord_tmd[:2]), int(ord_tmd[2:4]), int(ord_tmd[4:6])
            return datetime(base_date.year, base_date.month, base_date.day, h, m, s)
        except (ValueError, TypeError):
            return None

    @classmethod
    def calc_pnl(cls, entry_price: float, exit_price: float, quantity: int) -> tuple:
        """KIS 체결 기반 정확한 PnL 계산 (수수료+세금 포함).

        Returns:
            (pnl, pnl_pct) — 원 단위 손익, 퍼센트 수익률
        """
        buy_amount = entry_price * quantity
        sell_amount = exit_price * quantity
        buy_fee = buy_amount * cls.BUY_FEE_RATE
        sell_fee = sell_amount * cls.SELL_FEE_RATE
        sell_tax = sell_amount * cls.SELL_TAX_RATE
        cost = buy_amount + buy_fee
        net = sell_amount - sell_fee - sell_tax
        pnl = net - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
        return round(pnl), round(pnl_pct, 4)

    async def sync_from_kis(self, broker, engine=None):
        """
        KIS 당일 체결 내역과 캐시/DB 동기화.

        1) 누락 매수/매도 복구
        2) 청산 거래 PnL 보정 (수수료+세금 포함 정확한 값으로)
        절대 예외를 전파하지 않습니다.
        """
        try:
            if not hasattr(broker, "get_all_fills_for_date"):
                logger.debug("[TradeStorage] broker에 get_all_fills_for_date 없음, 동기화 건너뜀")
                return

            today = date.today()
            fills = await broker.get_all_fills_for_date(today)
            if not fills:
                logger.info("[TradeStorage] KIS 당일 체결 0건, 동기화 불필요")
                return

            # 캐시의 당일 거래
            cache_trades = {t.symbol: t for t in self.get_today_trades()}

            # KIS 체결을 종목별 매수/매도 그룹화
            buys = {}   # symbol → list of fills
            sells = {}  # symbol → list of fills
            for f in fills:
                side = f.get("sll_buy_dvsn_cd", "")
                sym = f.get("symbol", "")
                if not sym:
                    continue
                if side == "02":  # 매수
                    buys.setdefault(sym, []).append(f)
                elif side == "01":  # 매도
                    sells.setdefault(sym, []).append(f)

            synced = 0

            # DB에서 당일 이미 기록된 이벤트 조회 (캐시 불일치 방지)
            db_buy_symbols = set()
            db_sell_qty = {}  # trade_id → 총 매도 수량
            if self._db_available and self.pool:
                try:
                    buy_rows = await self.pool.fetch(
                        "SELECT DISTINCT symbol FROM trade_events WHERE event_type='BUY' AND event_time::date=$1",
                        today,
                    )
                    db_buy_symbols = {r['symbol'] for r in buy_rows}

                    sell_rows = await self.pool.fetch(
                        "SELECT trade_id, COALESCE(SUM(quantity), 0) as total_qty FROM trade_events WHERE event_type='SELL' AND event_time::date=$1 GROUP BY trade_id",
                        today,
                    )
                    db_sell_qty = {r['trade_id']: int(r['total_qty']) for r in sell_rows}
                except Exception as e:
                    logger.warning(f"[TradeStorage] DB 이벤트 조회 실패, 캐시 폴백: {e}")

            # 누락 매수 복구
            for sym, buy_fills in buys.items():
                if sym in cache_trades or sym in db_buy_symbols:
                    continue
                f = buy_fills[0]
                qty = int(f.get("tot_ccld_qty", 0))
                price = float(f.get("avg_prvs", 0))
                if qty <= 0 or price <= 0:
                    continue

                trade_id = f"KIS_SYNC_{sym}_{today.strftime('%Y%m%d')}"
                name = f.get("name", "") or f.get("prdt_name", "")
                # 엔진 포지션에서 전략 정보 추출
                strategy = "momentum_breakout"  # 장중 스크리닝 기본값
                if engine:
                    pos = engine.portfolio.positions.get(sym)
                    if pos and hasattr(pos, 'strategy') and pos.strategy:
                        strategy = str(pos.strategy.value) if hasattr(pos.strategy, 'value') else str(pos.strategy)
                self.record_entry(
                    trade_id=trade_id,
                    symbol=sym,
                    name=name or sym,
                    entry_price=price,
                    entry_quantity=qty,
                    entry_reason="KIS 동기화 복구",
                    entry_strategy=strategy,
                )
                synced += 1
                logger.info(f"[TradeStorage] KIS 동기화 매수 복구: {sym} {name} {qty}주 @ {price:,.0f}")

            # 누락 매도 복구
            for sym, sell_fills in sells.items():
                cache_trade = cache_trades.get(sym)
                if not cache_trade:
                    open_trades = [t for t in self.get_open_trades() if t.symbol == sym]
                    if not open_trades:
                        continue
                    open_trades.sort(key=lambda t: t.entry_time)  # FIFO 보장
                    cache_trade = open_trades[0]

                if cache_trade.is_closed:
                    continue

                # KIS 총 매도 수량 vs 이미 기록된 매도 수량 비교 (DB 우선, 캐시 폴백)
                kis_total_sold = sum(int(f.get("tot_ccld_qty", 0)) for f in sell_fills)
                db_sold = db_sell_qty.get(cache_trade.id, 0)
                already_sold = max(cache_trade.exit_quantity or 0, db_sold)
                missing_qty = kis_total_sold - already_sold

                if missing_qty <= 0:
                    logger.debug(f"[TradeStorage] {sym} 매도 이미 기록됨 (KIS={kis_total_sold}, 기록={already_sold})")
                    continue

                # 누락분만 복구 (마지막 체결가 사용)
                last_fill = sell_fills[-1]
                price = float(last_fill.get("avg_prvs", 0))
                if price <= 0:
                    continue

                actual_time = self._parse_kis_time(last_fill.get("ord_tmd", ""), today)

                self.record_exit(
                    trade_id=cache_trade.id,
                    exit_price=price,
                    exit_quantity=missing_qty,
                    exit_reason="KIS 동기화 복구",
                    exit_type="kis_sync",
                    exit_time=actual_time,
                )
                synced += 1
                logger.info(f"[TradeStorage] KIS 동기화 매도 복구: {sym} {missing_qty}주 @ {price:,.0f} (KIS={kis_total_sold}, 기존={already_sold})")

            if synced > 0:
                logger.info(f"[TradeStorage] KIS 동기화 완료: {synced}건 복구")
            else:
                logger.info("[TradeStorage] KIS 동기화 완료: 누락 없음")

            # PnL 보정 (수수료+세금 포함 정확한 값)
            await self._reconcile_pnl(today, sells)

        except Exception as e:
            logger.error(f"[TradeStorage] KIS 동기화 실패 (무시): {e}")

    async def _reconcile_pnl(self, target_date: date, kis_sells: Dict[str, list]):
        """
        당일 청산 거래의 PnL을 KIS 체결가 기준으로 보정.

        엔진 PnL은 수수료/세금을 제외하거나 부정확할 수 있으므로,
        KIS 실제 체결가와 DB 진입가를 기준으로 재계산합니다.
        """
        if not self._db_available or not self.pool:
            return

        try:
            # DB에서 당일 청산 거래 조회
            rows = await self.pool.fetch("""
                SELECT id, symbol, entry_price, exit_price, entry_quantity,
                       exit_quantity, pnl, pnl_pct
                FROM trades
                WHERE exit_time::date = $1 AND exit_time IS NOT NULL
            """, target_date)

            if not rows:
                return

            corrected = 0
            for row in rows:
                sym = row['symbol']
                entry_price = float(row['entry_price'])
                exit_qty = row['exit_quantity'] or 0

                if entry_price <= 0 or exit_qty <= 0:
                    continue

                # KIS 매도 체결가 사용 (있으면), 없으면 DB exit_price 그대로
                kis_exit_price = float(row['exit_price'])
                if sym in kis_sells:
                    # KIS의 가중평균 매도가 계산
                    total_qty = 0
                    total_amt = 0
                    for f in kis_sells[sym]:
                        q = int(f.get("tot_ccld_qty", 0))
                        p = float(f.get("avg_prvs", 0))
                        total_qty += q
                        total_amt += q * p
                    if total_qty > 0:
                        kis_exit_price = total_amt / total_qty

                # 수수료+세금 포함 정확한 PnL 계산
                correct_pnl, correct_pct = self.calc_pnl(entry_price, kis_exit_price, exit_qty)

                # 기존 값과 차이가 있으면 보정 (1원 이상 차이)
                old_pnl = float(row['pnl'] or 0)
                if abs(correct_pnl - old_pnl) < 1:
                    continue

                # 분할매도(SELL 이벤트 2건 이상) 시 trades 테이블 PnL 보정 건너뜀
                # — 개별 매도 이벤트의 PnL 합이 가중평균보다 정확함
                sell_count = await self.pool.fetchval("""
                    SELECT COUNT(*) FROM trade_events
                    WHERE trade_id = $1 AND event_type = 'SELL'
                """, row['id'])
                if sell_count and sell_count > 1:
                    logger.debug(f"[PnL보정] {sym} 분할매도 {sell_count}건 — trades 테이블 보정 건너뜀")
                    continue

                # DB 업데이트
                await self.pool.execute("""
                    UPDATE trades SET pnl = $1, pnl_pct = $2, exit_price = $3,
                                      updated_at = CURRENT_TIMESTAMP
                    WHERE id = $4
                """, correct_pnl, correct_pct, kis_exit_price, row['id'])

                # trade_events SELL 이벤트도 보정 (여기 도달 시 sell_count <= 1 보장)
                await self.pool.execute("""
                    UPDATE trade_events SET pnl = $1, pnl_pct = $2, price = $3
                    WHERE trade_id = $4 AND event_type = 'SELL'
                      AND event_time::date = $5
                """, correct_pnl, correct_pct, kis_exit_price, row['id'], target_date)

                # 캐시도 동기화
                cached = self._journal.get_trade(row['id'])
                if cached:
                    cached.pnl = Decimal(str(correct_pnl))
                    cached.pnl_pct = Decimal(str(correct_pct))

                corrected += 1
                logger.info(
                    f"[KIS보정] {sym} PnL 보정: {old_pnl:+,.0f} → {correct_pnl:+,.0f}원 "
                    f"(entry={entry_price:,.0f} exit={kis_exit_price:,.0f} qty={exit_qty})"
                )

            if corrected > 0:
                logger.info(f"[KIS보정] 당일 PnL 보정 완료: {corrected}건")

        except Exception as e:
            logger.error(f"[KIS보정] PnL 보정 실패 (무시): {e}")


# ── 싱글톤 팩토리 ──────────────────────────────────────────

_trade_storage: Optional[TradeStorage] = None


def get_trade_storage() -> TradeStorage:
    """TradeStorage 싱글톤 인스턴스 반환"""
    global _trade_storage
    if _trade_storage is None:
        _trade_storage = TradeStorage()
    return _trade_storage
