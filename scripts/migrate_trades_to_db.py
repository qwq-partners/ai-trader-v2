#!/usr/bin/env python3
"""
거래 데이터 JSON → PostgreSQL 마이그레이션 (1회성)

사용법:
    python scripts/migrate_trades_to_db.py

기존 trades_YYYYMMDD.json 파일을 trades + trade_events 테이블에 적재합니다.
ON CONFLICT DO NOTHING이므로 중복 실행 안전합니다.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg


JOURNAL_DIR = Path(os.path.expanduser("~/.cache/ai_trader/journal"))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ai_db")

# trade_storage.py의 SCHEMA_SQL과 동일
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


def parse_dt(s):
    """ISO datetime 문자열 → datetime 객체"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def safe_float(v, default=0.0):
    try:
        return float(v) if v else default
    except (ValueError, TypeError):
        return default


def safe_int(v, default=0):
    try:
        return int(v) if v else default
    except (ValueError, TypeError):
        return default


async def migrate():
    print(f"DB: {DATABASE_URL}")
    print(f"소스: {JOURNAL_DIR}")

    conn = await asyncpg.connect(DATABASE_URL)

    # 스키마 생성
    await conn.execute(SCHEMA_SQL)
    print("스키마 확인/생성 완료")

    json_files = sorted(JOURNAL_DIR.glob("trades_*.json"))
    if not json_files:
        print("마이그레이션 대상 JSON 파일 없음")
        await conn.close()
        return

    total_trades = 0
    total_events = 0

    for file_path in json_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  [SKIP] {file_path.name}: {e}")
            continue

        trades = data.get("trades", [])
        if not trades:
            continue

        for t in trades:
            trade_id = t.get("id", "")
            entry_time = parse_dt(t.get("entry_time"))
            if not trade_id or not entry_time:
                continue

            exit_time = parse_dt(t.get("exit_time"))
            created_at = parse_dt(t.get("created_at")) or entry_time
            updated_at = parse_dt(t.get("updated_at")) or entry_time

            # trades INSERT
            try:
                await conn.execute(
                    """INSERT INTO trades
                       (id, symbol, name, entry_time, entry_price, entry_quantity,
                        entry_reason, entry_strategy, entry_signal_score,
                        exit_time, exit_price, exit_quantity, exit_reason, exit_type,
                        pnl, pnl_pct, holding_minutes,
                        market_context, indicators_at_entry, indicators_at_exit, theme_info,
                        review_notes, lesson_learned, improvement_suggestion,
                        created_at, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26)
                       ON CONFLICT (id) DO NOTHING""",
                    trade_id,
                    t.get("symbol", ""),
                    t.get("name", ""),
                    entry_time,
                    safe_float(t.get("entry_price")),
                    safe_int(t.get("entry_quantity")),
                    t.get("entry_reason", ""),
                    t.get("entry_strategy", ""),
                    safe_float(t.get("entry_signal_score")),
                    exit_time,
                    safe_float(t.get("exit_price")),
                    safe_int(t.get("exit_quantity")),
                    t.get("exit_reason", ""),
                    t.get("exit_type", ""),
                    safe_float(t.get("pnl")),
                    safe_float(t.get("pnl_pct")),
                    safe_int(t.get("holding_minutes")),
                    json.dumps(t.get("market_context") or {}, default=str, ensure_ascii=False),
                    json.dumps(t.get("indicators_at_entry") or {}, default=str, ensure_ascii=False),
                    json.dumps(t.get("indicators_at_exit") or {}, default=str, ensure_ascii=False),
                    json.dumps(t.get("theme_info") or {}, default=str, ensure_ascii=False),
                    t.get("review_notes", ""),
                    t.get("lesson_learned", ""),
                    t.get("improvement_suggestion", ""),
                    created_at,
                    updated_at,
                )
                total_trades += 1
            except Exception as e:
                print(f"  [ERROR] trades INSERT {trade_id}: {e}")
                continue

            # BUY 이벤트 (중복 체크)
            is_closed = exit_time is not None
            entry_status = t.get("exit_type", "closed") if is_closed else "holding"
            try:
                exists = await conn.fetchval(
                    "SELECT 1 FROM trade_events WHERE trade_id=$1 AND event_type='BUY'",
                    trade_id,
                )
                if not exists:
                    await conn.execute(
                        """INSERT INTO trade_events
                           (trade_id, symbol, name, event_type, event_time, price, quantity,
                            strategy, signal_score, status)
                           VALUES ($1,$2,$3,'BUY',$4,$5,$6,$7,$8,$9)""",
                        trade_id,
                        t.get("symbol", ""),
                        t.get("name", ""),
                        entry_time,
                        safe_float(t.get("entry_price")),
                        safe_int(t.get("entry_quantity")),
                        t.get("entry_strategy", ""),
                        safe_float(t.get("entry_signal_score")),
                        entry_status,
                    )
                    total_events += 1
            except Exception as e:
                print(f"  [ERROR] BUY event {trade_id}: {e}")

            # SELL 이벤트 (청산된 거래만, 중복 체크)
            if is_closed:
                try:
                    exists = await conn.fetchval(
                        "SELECT 1 FROM trade_events WHERE trade_id=$1 AND event_type='SELL'",
                        trade_id,
                    )
                    if not exists:
                        await conn.execute(
                            """INSERT INTO trade_events
                               (trade_id, symbol, name, event_type, event_time, price, quantity,
                                exit_type, exit_reason, pnl, pnl_pct,
                                strategy, signal_score, status)
                               VALUES ($1,$2,$3,'SELL',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                            trade_id,
                            t.get("symbol", ""),
                            t.get("name", ""),
                            exit_time,
                            safe_float(t.get("exit_price")),
                            safe_int(t.get("exit_quantity")),
                            t.get("exit_type", ""),
                            t.get("exit_reason", ""),
                            safe_float(t.get("pnl")),
                            safe_float(t.get("pnl_pct")),
                            t.get("entry_strategy", ""),
                            safe_float(t.get("entry_signal_score")),
                            t.get("exit_type", "closed"),
                        )
                        total_events += 1
                except Exception as e:
                    print(f"  [ERROR] SELL event {trade_id}: {e}")

        print(f"  {file_path.name}: {len(trades)}건 처리")

    # 검증
    trade_count = await conn.fetchval("SELECT COUNT(*) FROM trades")
    event_count = await conn.fetchval("SELECT COUNT(*) FROM trade_events")

    await conn.close()

    print(f"\n=== 마이그레이션 완료 ===")
    print(f"처리: trades {total_trades}건, events {total_events}건")
    print(f"DB 검증: trades {trade_count}건, trade_events {event_count}건")


if __name__ == "__main__":
    asyncio.run(migrate())
