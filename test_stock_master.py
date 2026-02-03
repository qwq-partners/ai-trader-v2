#!/usr/bin/env python3
"""종목 마스터 DB 갱신 테스트"""

import asyncio
import sys
import os

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data.storage.stock_master import get_stock_master
from loguru import logger


async def test_refresh():
    """종목 마스터 갱신 테스트"""

    logger.info("=== 종목 마스터 갱신 테스트 ===")

    # 1. 인스턴스 생성
    stock_master = get_stock_master()

    # 2. 연결
    await stock_master.connect()

    # 3. 갱신
    logger.info("종목 마스터 갱신 시작...")
    stats = await stock_master.refresh_master()

    # 4. 통계 출력
    logger.info(f"갱신 완료:")
    logger.info(f"  - 전체: {stats.get('total', 0)}개")
    logger.info(f"  - KOSPI: {stats.get('KOSPI', 0)}개")
    logger.info(f"  - KOSDAQ: {stats.get('KOSDAQ', 0)}개")
    logger.info(f"  - ETF: {stats.get('ETF', 0)}개")
    logger.info(f"  - KOSPI200: {stats.get('KOSPI200', 0)}개")
    logger.info(f"  - KOSDAQ150: {stats.get('KOSDAQ150', 0)}개")

    # 5. 삼성전자 조회 테스트
    logger.info("\n삼성전자 조회 테스트:")
    ticker = await stock_master.lookup_ticker("삼성전자")
    if ticker:
        logger.info(f"  ✓ 삼성전자 → {ticker}")
        is_valid = await stock_master.validate_ticker(ticker)
        logger.info(f"  ✓ {ticker} 검증: {is_valid}")
    else:
        logger.error("  ✗ 삼성전자 조회 실패")

    # 6. 대표 종목 조회 테스트
    logger.info("\n대표 종목 조회 (상위 10개):")
    top_stocks = await stock_master.get_top_stocks(10)
    for stock_hint in top_stocks[:10]:
        logger.info(f"  - {stock_hint}")

    # 7. 연결 종료
    await stock_master.disconnect()

    logger.info("\n=== 테스트 완료 ===")


if __name__ == "__main__":
    asyncio.run(test_refresh())
