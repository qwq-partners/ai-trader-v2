#!/usr/bin/env python3
"""
종목 마스터 1회 갱신 스크립트

전체 종목 데이터(KOSPI/KOSDAQ/KONEX/ETF)를 DB에 로드합니다.
"""
import asyncio
import os
import sys
from pathlib import Path

# 프로젝트 루트를 PYTHONPATH에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# .env 파일 로드 (python-dotenv 없이)
env_file = project_root / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

from src.data.storage.stock_master import get_stock_master


async def main():
    """종목 마스터 갱신 실행"""
    print("=== 종목 마스터 갱신 시작 ===\n")

    # DB URL 환경변수에서 로드
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ ERROR: DATABASE_URL 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    # StockMaster 인스턴스 생성 및 연결
    stock_master = get_stock_master(db_url)
    await stock_master.connect()

    try:
        # 종목 마스터 갱신
        print("📊 종목 데이터 로드 중... (약 30초 소요)")
        stats = await stock_master.refresh_master()

        print("\n✅ 갱신 완료!")
        print(f"\n📈 통계:")
        print(f"  - 전체 종목: {stats.get('total', 0):,}개")
        print(f"  - KOSPI: {stats.get('KOSPI', 0):,}개")
        print(f"  - KOSDAQ: {stats.get('KOSDAQ', 0):,}개")
        print(f"  - ETF: {stats.get('ETF', 0):,}개")
        print(f"  - KOSPI200: {stats.get('KOSPI200', 0):,}개")
        print(f"  - KOSDAQ150: {stats.get('KOSDAQ150', 0):,}개")

        # 삼성전자 조회 검증
        print("\n🔍 삼성전자(005930) 조회 검증:")
        ticker = await stock_master.lookup_ticker("삼성전자")
        if ticker == "005930":
            print(f"  ✓ 종목명 조회: '삼성전자' → {ticker}")
        else:
            print(f"  ✗ 종목명 조회 실패: {ticker}")

        is_valid = await stock_master.validate_ticker("005930")
        if is_valid:
            print(f"  ✓ 코드 검증: '005930' → 유효")
        else:
            print(f"  ✗ 코드 검증 실패")

        # KOSPI200 주요 종목 샘플
        print("\n📋 KOSPI200 + KOSDAQ150 상위 10개:")
        top_stocks = await stock_master.get_top_stocks(limit=10)
        for i, stock in enumerate(top_stocks, 1):
            print(f"  {i:2d}. {stock}")

    finally:
        await stock_master.disconnect()
        print("\n=== 완료 ===")


if __name__ == "__main__":
    asyncio.run(main())
