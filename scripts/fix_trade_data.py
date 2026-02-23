#!/usr/bin/env python3
"""
DB 거래 데이터 정리 스크립트

수정 대상:
1. exit_quantity 교정 — trade_events SELL 합산 기준
2. PnL 재계산 — 수수료+세금 포함 정확한 값
3. 종목명 보정 — stock_master에서 올바른 이름 조회

사용법:
    python scripts/fix_trade_data.py              # dry-run (변경 없음)
    python scripts/fix_trade_data.py --apply      # 실제 적용
"""

import asyncio
import argparse
import os
import sys

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

try:
    from pykrx import stock as pykrx_stock
    PYKRX_AVAILABLE = True
except ImportError:
    PYKRX_AVAILABLE = False

from src.data.storage.trade_storage import TradeStorage


async def fix_exit_quantities(conn, apply: bool) -> int:
    """exit_quantity 교정: trade_events SELL 합산 기준"""
    print("\n=== 1. exit_quantity 교정 ===")

    rows = await conn.fetch("""
        SELECT t.id, t.symbol, t.name, t.entry_quantity, t.exit_quantity,
               COALESCE(
                   (SELECT SUM(te.quantity) FROM trade_events te
                    WHERE te.trade_id = t.id AND te.event_type = 'SELL'),
                   0
               ) as actual_sold
        FROM trades t
        WHERE t.exit_time IS NOT NULL
    """)

    fixes = 0
    for r in rows:
        actual_sold = int(r['actual_sold'])
        current_exit = r['exit_quantity'] or 0
        entry_qty = r['entry_quantity']

        # actual_sold가 entry_quantity 이상이면 완전 청산
        expected_exit = min(actual_sold, entry_qty) if actual_sold >= entry_qty else actual_sold

        if current_exit != expected_exit:
            print(f"  {r['symbol']} ({r['name']}): exit_qty {current_exit} → {expected_exit} "
                  f"(entry={entry_qty}, events_sold={actual_sold})")
            if apply:
                await conn.execute(
                    "UPDATE trades SET exit_quantity = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
                    expected_exit, r['id']
                )
            fixes += 1

    print(f"  → {fixes}건 {'수정 완료' if apply else '수정 필요'}")
    return fixes


async def fix_pnl(conn, apply: bool) -> int:
    """PnL 재계산: 수수료+세금 포함"""
    print("\n=== 2. PnL 재계산 ===")

    rows = await conn.fetch("""
        SELECT t.id, t.symbol, t.name, t.entry_price, t.entry_quantity,
               t.exit_quantity, t.pnl, t.pnl_pct
        FROM trades t
        WHERE t.exit_time IS NOT NULL
          AND t.entry_price > 0
          AND t.exit_quantity > 0
    """)

    fixes = 0
    for r in rows:
        trade_id = r['id']
        entry_price = float(r['entry_price'])
        exit_qty = r['exit_quantity'] or 0

        if exit_qty <= 0:
            continue

        # trade_events SELL에서 가중평균 매도가 계산
        sell_events = await conn.fetch("""
            SELECT price, quantity FROM trade_events
            WHERE trade_id = $1 AND event_type = 'SELL'
        """, trade_id)

        if not sell_events:
            continue

        total_sell_amt = sum(float(se['price']) * int(se['quantity']) for se in sell_events)
        total_sell_qty = sum(int(se['quantity']) for se in sell_events)

        if total_sell_qty <= 0:
            continue

        avg_sell_price = total_sell_amt / total_sell_qty
        calc_qty = min(exit_qty, total_sell_qty)

        # 수수료+세금 포함 PnL 계산
        correct_pnl, correct_pct = TradeStorage.calc_pnl(entry_price, avg_sell_price, calc_qty)

        old_pnl = float(r['pnl'] or 0)
        old_pct = float(r['pnl_pct'] or 0)

        # 1원 이상 차이 또는 소수점 잔존
        has_decimal = old_pnl != round(old_pnl)
        has_diff = abs(correct_pnl - old_pnl) >= 1

        if has_decimal or has_diff:
            reason = "소수점" if has_decimal and not has_diff else "PnL차이"
            if has_decimal and has_diff:
                reason = "소수점+PnL차이"
            print(f"  {r['symbol']} ({r['name']}): pnl {old_pnl:+,.1f} → {correct_pnl:+,d} "
                  f"pct {old_pct:+.2f}% → {correct_pct:+.4f}% [{reason}]")
            if apply:
                await conn.execute(
                    "UPDATE trades SET pnl = $1, pnl_pct = $2, updated_at = CURRENT_TIMESTAMP WHERE id = $3",
                    correct_pnl, correct_pct, trade_id
                )
            fixes += 1

    print(f"  → {fixes}건 {'수정 완료' if apply else '수정 필요'}")
    return fixes


async def fix_names(conn, apply: bool) -> int:
    """종목명 보정: 코드만 저장된 경우 pykrx에서 조회"""
    print("\n=== 3. 종목명 보정 ===")

    if not PYKRX_AVAILABLE:
        print("  pykrx 미설치, 건너뜀")
        return 0

    # 종목명이 심볼과 동일하거나 비어있는 거래
    rows = await conn.fetch("""
        SELECT DISTINCT symbol, name FROM trades
        WHERE name = symbol OR name = '' OR name IS NULL
    """)

    if not rows:
        print("  → 보정 필요한 종목 없음")
        return 0

    fixes = 0
    for r in rows:
        sym = r['symbol']
        try:
            real_name = pykrx_stock.get_market_ticker_name(sym)
            if real_name and real_name != sym:
                print(f"  {sym}: '{r['name']}' → '{real_name}'")
                if apply:
                    await conn.execute(
                        "UPDATE trades SET name = $1, updated_at = CURRENT_TIMESTAMP "
                        "WHERE symbol = $2 AND (name = $2 OR name = '' OR name IS NULL)",
                        real_name, sym
                    )
                    await conn.execute(
                        "UPDATE trade_events SET name = $1 "
                        "WHERE symbol = $2 AND (name = $2 OR name = '' OR name IS NULL)",
                        real_name, sym
                    )
                fixes += 1
        except Exception as e:
            print(f"  {sym}: 조회 실패 ({e})")

    print(f"  → {fixes}건 {'수정 완료' if apply else '수정 필요'}")
    return fixes


async def show_summary(conn):
    """현재 DB 상태 요약"""
    print("\n=== DB 현황 ===")

    total = await conn.fetchval("SELECT COUNT(*) FROM trades")
    closed = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE exit_time IS NOT NULL")
    open_cnt = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE exit_time IS NULL")
    events = await conn.fetchval("SELECT COUNT(*) FROM trade_events")

    # 소수점 PnL 잔존
    decimal_pnl = await conn.fetchval(
        "SELECT COUNT(*) FROM trades WHERE pnl != ROUND(pnl, 0) AND exit_time IS NOT NULL"
    )

    print(f"  총 거래: {total} (청산: {closed}, 미청산: {open_cnt})")
    print(f"  이벤트: {events}")
    print(f"  소수점 PnL 잔존: {decimal_pnl}건")


async def main():
    parser = argparse.ArgumentParser(description="DB 거래 데이터 정리")
    parser.add_argument("--apply", action="store_true", help="실제 수정 적용 (기본: dry-run)")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    print(f"모드: {'APPLY (실제 수정)' if args.apply else 'DRY-RUN (변경 없음)'}")
    print(f"DB: {db_url[:30]}...")

    conn = await asyncpg.connect(db_url)
    try:
        await show_summary(conn)

        total_fixes = 0
        total_fixes += await fix_exit_quantities(conn, args.apply)
        total_fixes += await fix_pnl(conn, args.apply)
        total_fixes += await fix_names(conn, args.apply)

        print(f"\n=== 완료: 총 {total_fixes}건 {'수정됨' if args.apply else '수정 필요'} ===")

        if args.apply:
            await show_summary(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(main())
