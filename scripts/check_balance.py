#!/usr/bin/env python3
"""
AI Trading Bot v2 - 계좌 잔고 확인 스크립트

KIS API를 사용하여 현재 계좌 잔고와 보유 종목을 확인합니다.

사용법:
    python scripts/check_balance.py
"""

import asyncio
import os
import sys
from pathlib import Path
from decimal import Decimal

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from loguru import logger

# 환경변수 로드
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


async def main():
    """메인 함수"""
    # 로거 설정
    logger.remove()
    logger.add(sys.stdout, format="{message}", level="INFO")

    print("=" * 60)
    print("AI Trading Bot v2 - 계좌 잔고 확인")
    print("=" * 60)

    try:
        # 브로커 초기화
        config = KISConfig.from_env()
        broker = KISBroker(config)

        if not await broker.connect():
            print("\n[오류] 브로커 연결 실패")
            return

        # 계좌 잔고 조회
        print("\n[계좌 정보]")
        print("-" * 40)

        balance = await broker.get_account_balance()
        if balance:
            print(f"총 자산:      {balance.get('total_equity', 0):>15,.0f}원  (주문가능+주식)")
            print(f"  주문가능:   {balance.get('available_cash', 0):>15,.0f}원")
            print(f"  주식평가:   {balance.get('stock_value', 0):>15,.0f}원")
            print(f"매입 금액:    {balance.get('purchase_amount', 0):>15,.0f}원")
            print(f"평가 손익:    {balance.get('unrealized_pnl', 0):>+15,.0f}원")
            print("-" * 40)
            print(f"예수금(D+2): {balance.get('deposit', 0):>15,.0f}원  (미정산 포함)")
            print(f"KIS 총평가:   {balance.get('tot_evlu_amt', 0):>15,.0f}원  (참고용)")
        else:
            print("잔고 정보를 가져올 수 없습니다.")

        # 보유 종목 조회
        print("\n[보유 종목]")
        print("-" * 60)

        positions = await broker.get_positions()
        if positions:
            print(f"{'종목코드':<10} {'보유수량':>10} {'평균단가':>12} {'현재가':>12} {'손익률':>10}")
            print("-" * 60)

            total_pnl = Decimal("0")
            for symbol, pos in positions.items():
                if pos.quantity > 0:
                    pnl_pct = pos.unrealized_pnl_pct
                    pnl = pos.unrealized_pnl
                    total_pnl += pnl

                    pnl_str = f"{pnl_pct:+.2f}%"
                    print(
                        f"{symbol:<10} {pos.quantity:>10,}주 "
                        f"{float(pos.avg_price):>12,.0f} "
                        f"{float(pos.current_price):>12,.0f} "
                        f"{pnl_str:>10}"
                    )

            print("-" * 60)
            print(f"{'총 미실현 손익':>44}: {total_pnl:>+12,.0f}원")
        else:
            print("보유 종목이 없습니다.")

        # 연결 해제
        await broker.disconnect()

    except Exception as e:
        print(f"\n[오류] {e}")
        raise

    print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
