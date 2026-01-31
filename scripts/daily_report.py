#!/usr/bin/env python3
"""
AI Trading Bot v2 - 일일 리포트 생성 스크립트

사용법:
    python scripts/daily_report.py [--date YYYY-MM-DD] [--no-ai]
"""

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

# 프로젝트 루트를 path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from loguru import logger

from src.utils.config import AppConfig
from src.utils.logger import setup_logger
from analytics.reporter import (
    DailyReporter,
    TradeRecord,
    generate_daily_report,
)


def parse_args():
    parser = argparse.ArgumentParser(description="일일 트레이딩 리포트 생성")
    parser.add_argument(
        "--date", "-d",
        type=str,
        default=None,
        help="리포트 날짜 (YYYY-MM-DD 형식, 기본: 오늘)"
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="AI 분석 제외"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="출력 파일 경로"
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    # 로거 설정
    log_dir = project_root / "logs" / datetime.now().strftime("%Y%m%d")
    setup_logger(
        log_level="INFO",
        log_dir=str(log_dir),
        enable_console=True,
        enable_file=False,
    )

    # 설정 로드
    config = AppConfig.load(
        config_path=None,
        dotenv_path=str(project_root / ".env")
    )

    # 날짜 파싱
    target_date = date.today()
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"잘못된 날짜 형식: {args.date}")
            return

    logger.info(f"일일 리포트 생성: {target_date}")

    # 리포터 초기화
    reporter = DailyReporter()
    reporter.set_initial_capital(config.trading.initial_capital)

    # 샘플 데이터 (실제로는 DB나 로그에서 로드)
    # TODO: 실제 거래 데이터 로드 구현

    # 리포트 생성
    include_ai = not args.no_ai
    report = await reporter.generate_report(include_ai_analysis=include_ai)

    # 출력
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(report, encoding="utf-8")
        logger.info(f"리포트 저장: {output_path}")
    else:
        print(report)


if __name__ == "__main__":
    asyncio.run(main())
