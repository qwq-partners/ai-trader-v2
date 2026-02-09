#!/usr/bin/env python3
"""
전략 전환을 위한 보유 포지션 전량 청산

스윙 모멘텀 전환을 위해 기존 데이트레이딩 포지션을
화요일 장 시작 직후 시장가 매도로 일괄 청산합니다.

사용법:
    python scripts/liquidate_all.py           # 대화형 (확인 후 실행)
    python scripts/liquidate_all.py --force   # 확인 없이 실행
    python scripts/liquidate_all.py --dry-run # 포지션 조회만 (주문 없음)
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from loguru import logger


# 환경변수 로드 (check_balance.py 동일 패턴)
def load_env():
    env_path = project_root / ".env"
    if env_path.exists():
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = value


load_env()

from src.execution.broker.kis_broker import KISBroker, KISConfig
from src.core.types import Order, OrderSide, OrderType


def check_market_hours() -> bool:
    """정규장 시간(09:00~15:20) 여부 확인"""
    now = datetime.now()
    hour, minute = now.hour, now.minute
    current_minutes = hour * 60 + minute
    # 09:00 = 540분, 15:20 = 920분
    return 540 <= current_minutes <= 920


def parse_args():
    parser = argparse.ArgumentParser(description="보유 포지션 전량 청산")
    parser.add_argument("--force", action="store_true", help="확인 없이 즉시 실행")
    parser.add_argument("--dry-run", action="store_true", help="포지션 조회만 (주문 제출 안 함)")
    return parser.parse_args()


async def send_telegram(message: str):
    """텔레그램 알림 (선택)"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"chat_id": chat_id, "text": message})
    except Exception as e:
        print(f"[텔레그램 알림 실패] {e}")


async def main():
    args = parse_args()

    # 로거 설정
    logger.remove()
    logger.add(sys.stdout, format="{time:HH:mm:ss} | {message}", level="INFO")

    print("=" * 60)
    print("전략 전환 - 보유 포지션 전량 청산")
    print(f"실행 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. 시간 체크
    if not args.dry_run and not check_market_hours():
        print("\n[거부] 정규장 시간(09:00~15:20)이 아닙니다.")
        print("       현재 시간에는 시장가 매도를 실행할 수 없습니다.")
        return

    # 2. 브로커 초기화
    try:
        config = KISConfig.from_env()
        broker = KISBroker(config)
        if not await broker.connect():
            print("\n[오류] 브로커 연결 실패")
            return
    except Exception as e:
        print(f"\n[오류] 브로커 초기화 실패: {e}")
        return

    try:
        # 3. 보유 포지션 조회
        positions = await broker.get_positions()
        active_positions = {s: p for s, p in positions.items() if p.quantity > 0}

        if not active_positions:
            print("\n보유 종목이 없습니다. 이미 전량 청산 완료.")
            await broker.disconnect()
            return

        # 포지션 표시
        print(f"\n[보유 종목: {len(active_positions)}건]")
        print("-" * 65)
        print(f"{'종목코드':<10} {'종목명':<14} {'수량':>6} {'평균단가':>10} {'현재가':>10} {'손익률':>8}")
        print("-" * 65)

        total_value = Decimal("0")
        total_pnl = Decimal("0")
        for symbol, pos in active_positions.items():
            pnl_pct = pos.unrealized_pnl_pct
            total_value += pos.market_value
            total_pnl += pos.unrealized_pnl
            print(
                f"{symbol:<10} {pos.name:<14} {pos.quantity:>6}주 "
                f"{float(pos.avg_price):>10,.0f} "
                f"{float(pos.current_price):>10,.0f} "
                f"{pnl_pct:>+7.2f}%"
            )

        print("-" * 65)
        print(f"총 평가금액: {total_value:>12,.0f}원  |  총 미실현 손익: {total_pnl:>+12,.0f}원")

        # dry-run이면 여기서 종료
        if args.dry_run:
            print("\n[dry-run] 주문 제출 없이 종료합니다.")
            await broker.disconnect()
            return

        # 4. 확인 프롬프트
        if not args.force:
            print(f"\n위 {len(active_positions)}건을 시장가 매도로 전량 청산합니다.")
            confirm = input("계속하시겠습니까? (y/N): ").strip().lower()
            if confirm != 'y':
                print("취소되었습니다.")
                await broker.disconnect()
                return

        # 5. 1차: 매수1호가 지정가 매도 주문 제출
        print(f"\n[1차: 매수1호가 지정가 매도 주문 제출 중...]")
        results = []
        for symbol, pos in active_positions.items():
            bid_price = await broker.get_best_bid(symbol)
            if bid_price:
                order = Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=pos.quantity,
                    price=Decimal(str(bid_price)),
                    reason="전략전환 전량청산",
                )
                price_info = f"지정가 {bid_price:,.0f}"
            else:
                order = Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=pos.quantity,
                    reason="전략전환 전량청산 (호가조회실패)",
                )
                price_info = "시장가 (호가조회실패)"
            success, order_id = await broker.submit_order(order)
            status = "성공" if success else "실패"
            results.append((symbol, pos.name, pos.quantity, success, order_id))
            print(f"  {symbol} {pos.name} {pos.quantity}주 → {status} [{price_info}] ({order_id})")
            await asyncio.sleep(0.5)  # API rate limit

        # 6. 15초 대기 후 미체결 확인 → 시장가 폴백
        print("\n[15초 대기 후 미체결 확인...]")
        await asyncio.sleep(15)
        remaining = await broker.get_positions()
        remaining_active = {s: p for s, p in remaining.items() if p.quantity > 0}

        if remaining_active:
            print(f"\n[2차: 미체결 {len(remaining_active)}종목 시장가 전환]")
            # 기존 지정가 취소
            for symbol in remaining_active:
                try:
                    await broker.cancel_all_for_symbol(symbol)
                except Exception as e:
                    print(f"  {symbol} 취소 실패: {e}")
            await asyncio.sleep(1)
            # 시장가 재주문
            for symbol, pos in remaining_active.items():
                order = Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=pos.quantity,
                    reason="전략전환 전량청산 (미체결 폴백)",
                )
                success, order_id = await broker.submit_order(order)
                status = "성공" if success else "실패"
                print(f"  {symbol} {pos.name} {pos.quantity}주 → 시장가 {status} ({order_id})")
                await asyncio.sleep(0.5)

        # 7. 체결 대기 (30초 간격, 최대 3분)
        print("\n[체결 대기 중...]")
        max_wait = 180  # 3분
        interval = 30
        waited = 0
        all_cleared = False

        while waited < max_wait:
            await asyncio.sleep(interval)
            waited += interval
            remaining = await broker.get_positions()
            remaining_active = {s: p for s, p in remaining.items() if p.quantity > 0}

            if not remaining_active:
                all_cleared = True
                print(f"  {waited}초 경과 - 전량 체결 완료!")
                break
            else:
                symbols_left = ", ".join(remaining_active.keys())
                print(f"  {waited}초 경과 - 잔여: {symbols_left}")

        # 7. 최종 결과
        print("\n" + "=" * 60)
        if all_cleared:
            print("[완료] 모든 포지션 청산 완료!")
        else:
            final_positions = await broker.get_positions()
            final_active = {s: p for s, p in final_positions.items() if p.quantity > 0}
            if not final_active:
                print("[완료] 모든 포지션 청산 완료!")
                all_cleared = True
            else:
                print("[경고] 일부 포지션 미체결:")
                for symbol, pos in final_active.items():
                    print(f"  {symbol} {pos.name} 잔여 {pos.quantity}주")

        # 8. 텔레그램 알림
        msg_lines = ["[전략전환] 전량 청산 결과"]
        for symbol, name, qty, success, oid in results:
            msg_lines.append(f"  {'✓' if success else '✗'} {name}({symbol}) {qty}주")
        msg_lines.append(f"상태: {'전량 체결' if all_cleared else '일부 미체결'}")
        await send_telegram("\n".join(msg_lines))

        print("=" * 60)

    finally:
        await broker.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
