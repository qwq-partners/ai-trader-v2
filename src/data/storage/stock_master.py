"""
AI Trading Bot v2 - 종목 마스터 DB

KRX 전체 종목 정보를 DB에 저장하고 관리
- FinanceDataReader로 KOSPI/KOSDAQ/KONEX/ETF 로드
- pykrx로 KOSPI200/KOSDAQ150 여부 판별
- 종목명 → 코드 변환, 코드 검증 기능
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor
from loguru import logger
import asyncpg


# 전역 싱글톤
_stock_master_instance: Optional["StockMaster"] = None
_executor = ThreadPoolExecutor(max_workers=2)


class StockMaster:
    """
    종목 마스터 DB 관리

    테이블 스키마:
        kr_stock_master (
            ticker VARCHAR(10) PRIMARY KEY,
            corp_name VARCHAR(200) NOT NULL,
            market VARCHAR(20) NOT NULL,
            corp_cls VARCHAR(10) DEFAULT '',
            kospi200_yn VARCHAR(1) DEFAULT 'N',
            kosdaq150_yn VARCHAR(1) DEFAULT 'N',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """

    def __init__(self, db_url: str):
        self.db_url = db_url
        self.pool: Optional[asyncpg.Pool] = None

        # 인메모리 캐시
        self._name_cache: Dict[str, str] = {}  # 종목명 → 코드
        self._ticker_set: Set[str] = set()  # 전체 코드 집합
        self._etf_set: Set[str] = set()  # ETF/ETN 코드 집합 (market='ETF' or corp_cls='ETF')
        self._cache_loaded = False

    async def connect(self) -> bool:
        """DB 연결. 성공 시 True 반환."""
        if self.pool:
            return True

        try:
            self.pool = await asyncpg.create_pool(
                self.db_url,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )
            logger.info("[StockMaster] DB 연결 완료")
            await self._ensure_table()
            await self._load_cache()
            return True
        except Exception as e:
            logger.error(f"[StockMaster] DB 연결 실패: {e}")
            return False

    async def disconnect(self):
        """DB 연결 종료"""
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("[StockMaster] DB 연결 종료")

    async def is_empty(self) -> bool:
        """테이블이 비어있는지 확인"""
        await self._ensure_connected()
        count = await self.pool.fetchval("SELECT COUNT(*) FROM kr_stock_master")
        return (count or 0) == 0

    async def rebuild_cache(self) -> None:
        """인메모리 캐시만 재구축 (FDR/pykrx API 호출 없음)"""
        await self._load_cache()
        logger.info("[StockMaster] 캐시 재구축 완료")

    async def _ensure_connected(self):
        """연결 확인"""
        if not self.pool:
            raise RuntimeError("StockMaster not connected. Call connect() first.")

    async def _ensure_table(self):
        """테이블 생성"""
        await self._ensure_connected()

        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS kr_stock_master (
                    ticker VARCHAR(10) PRIMARY KEY,
                    corp_name VARCHAR(200) NOT NULL,
                    market VARCHAR(20) NOT NULL,
                    corp_cls VARCHAR(10) DEFAULT '',
                    kospi200_yn VARCHAR(1) DEFAULT 'N',
                    kosdaq150_yn VARCHAR(1) DEFAULT 'N',
                    kospi500_yn VARCHAR(1) DEFAULT 'N',
                    market_cap BIGINT DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 신규 컬럼 마이그레이션 (기존 테이블에 없을 경우 자동 추가)
            for col_ddl in [
                "ALTER TABLE kr_stock_master ADD COLUMN IF NOT EXISTS kospi500_yn VARCHAR(1) DEFAULT 'N'",
                "ALTER TABLE kr_stock_master ADD COLUMN IF NOT EXISTS market_cap BIGINT DEFAULT 0",
            ]:
                try:
                    await conn.execute(col_ddl)
                except Exception:
                    pass  # 이미 존재하면 무시

            # 인덱스
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_corp_name
                ON kr_stock_master(corp_name)
            """)

            logger.debug("[StockMaster] 테이블 확인 완료")

    async def _load_cache(self):
        """인메모리 캐시 로드"""
        await self._ensure_connected()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT ticker, corp_name, market, corp_cls FROM kr_stock_master")

            self._name_cache = {row["corp_name"]: row["ticker"] for row in rows}
            self._ticker_set = {row["ticker"] for row in rows}
            # ETF/ETN 코드 집합: market='ETF' 또는 corp_cls가 'ETF'인 종목
            self._etf_set = {
                row["ticker"] for row in rows
                if row["market"] == "ETF" or (row["corp_cls"] or "").upper() == "ETF"
            }
            self._cache_loaded = True

            logger.info(
                f"[StockMaster] 캐시 로드: {len(self._ticker_set)}개 종목 "
                f"(ETF/ETN: {len(self._etf_set)}개)"
            )

    @staticmethod
    def _sync_load_fdr() -> List[Dict]:
        """FinanceDataReader로 종목 로드 (동기)"""
        try:
            import FinanceDataReader as fdr
        except ImportError:
            logger.error("[StockMaster] FinanceDataReader 설치 필요: pip install finance-datareader")
            return []

        stocks = []

        for market in ["KOSPI", "KOSDAQ", "KONEX"]:
            try:
                df = fdr.StockListing(market)
                for _, row in df.iterrows():
                    stocks.append({
                        "ticker": str(row.get("Code") or row.get("Symbol", "")).strip(),
                        "corp_name": str(row.get("Name", "")).strip(),
                        "market": market,
                        "corp_cls": str(row.get("Sector", "")).strip()[:10],
                    })
                logger.info(f"[StockMaster/FDR] {market} {len(df)}개 종목 로드")
            except Exception as e:
                logger.warning(f"[StockMaster/FDR] {market} 로드 실패: {e}")

        # ETF/ETN
        try:
            df = fdr.StockListing("ETF/KR")
            for _, row in df.iterrows():
                stocks.append({
                    "ticker": str(row.get("Code") or row.get("Symbol", "")).strip(),
                    "corp_name": str(row.get("Name", "")).strip(),
                    "market": "ETF",
                    "corp_cls": "ETF",
                })
            logger.info(f"[StockMaster/FDR] ETF {len(df)}개 종목 로드")
        except Exception as e:
            logger.warning(f"[StockMaster/FDR] ETF 로드 실패: {e}")

        return stocks

    @staticmethod
    def _sync_load_index_members() -> Tuple[Set[str], Set[str], Set[str], Dict[str, int]]:
        """
        pykrx로 지수 구성 종목 + 시가총액 로드 (동기)

        Returns:
            kospi200: KOSPI200 구성 종목 코드 집합
            kospi500: KOSPI500 구성 종목 코드 집합 (KOSPI200 포함)
            kosdaq150: KOSDAQ150 구성 종목 코드 집합
            market_caps: {ticker: 시가총액(억원)} 딕셔너리
        """
        kospi200: Set[str] = set()
        kospi500: Set[str] = set()
        kosdaq150: Set[str] = set()
        market_caps: Dict[str, int] = {}

        try:
            from pykrx import stock
            from datetime import datetime as _dt

            today_str = _dt.now().strftime("%Y%m%d")

            # KOSPI200
            try:
                tickers = stock.get_index_portfolio_deposit_file("1028")
                if tickers:
                    kospi200 = set(tickers)
                    logger.info(f"[StockMaster/pykrx] KOSPI200 {len(kospi200)}개 종목")
            except Exception as e:
                logger.warning(f"[StockMaster/pykrx] KOSPI200 로드 실패: {e}")

            # KOSPI500: pykrx 공식 코드 미지원 → 시총 기준 상위 500개로 대체
            # (시가총액 로드 후 아래에서 결정)

            # KOSDAQ150
            try:
                tickers = stock.get_index_portfolio_deposit_file("2203")
                if tickers:
                    kosdaq150 = set(tickers)
                    logger.info(f"[StockMaster/pykrx] KOSDAQ150 {len(kosdaq150)}개 종목")
            except Exception as e:
                logger.warning(f"[StockMaster/pykrx] KOSDAQ150 로드 실패: {e}")

            # 시가총액 (KOSPI + KOSDAQ 전체) + KOSPI500 계산
            try:
                df_kospi = stock.get_market_cap_by_ticker(today_str, market="KOSPI")
                df_kosdaq = stock.get_market_cap_by_ticker(today_str, market="KOSDAQ")

                cap_col = None
                for df_tmp in [df_kospi, df_kosdaq]:
                    if df_tmp is not None and not df_tmp.empty:
                        for col in ["시가총액", "Marcap", "marcap"]:
                            if col in df_tmp.columns:
                                cap_col = col
                                break
                    if cap_col:
                        break

                if cap_col:
                    # 시총 딕셔너리 구성
                    kospi_caps: Dict[str, int] = {}
                    kosdaq_caps: Dict[str, int] = {}

                    if df_kospi is not None and not df_kospi.empty:
                        for ticker, row in df_kospi.iterrows():
                            cap_won = int(row[cap_col])
                            cap_eok = cap_won // 100_000_000  # 억원
                            market_caps[str(ticker)] = cap_eok
                            kospi_caps[str(ticker)] = cap_eok

                    if df_kosdaq is not None and not df_kosdaq.empty:
                        for ticker, row in df_kosdaq.iterrows():
                            cap_won = int(row[cap_col])
                            cap_eok = cap_won // 100_000_000
                            market_caps[str(ticker)] = cap_eok
                            kosdaq_caps[str(ticker)] = cap_eok

                    logger.info(
                        f"[StockMaster/pykrx] 시가총액 로드: "
                        f"KOSPI {len(kospi_caps)}개, KOSDAQ {len(kosdaq_caps)}개"
                    )

                    # KOSPI500: KOSPI 시총 상위 500개
                    kospi500_list = sorted(kospi_caps, key=lambda t: kospi_caps[t], reverse=True)[:500]
                    kospi500 = set(kospi500_list)
                    logger.info(f"[StockMaster/pykrx] KOSPI500(시총기준) {len(kospi500)}개 종목")

            except Exception as e:
                logger.warning(f"[StockMaster/pykrx] 시가총액 로드 실패: {e}")

        except ImportError:
            logger.warning("[StockMaster] pykrx 설치 필요: pip install pykrx")

        # pykrx 실패 시 FDR 폴백 (KOSPI200만)
        if not kospi200:
            try:
                import FinanceDataReader as fdr
                df = fdr.StockListing("KRX-MARCAP")
                if not df.empty and "KOSPI200" in df.columns:
                    kospi200 = set(df[df["KOSPI200"] == "Y"]["Code"].astype(str))
                    logger.info(f"[StockMaster/FDR폴백] KOSPI200 {len(kospi200)}개")
            except Exception:
                pass

        return kospi200, kospi500, kosdaq150, market_caps

    # 하위 호환 alias
    @staticmethod
    def _sync_load_kospi200_kosdaq150() -> Tuple[Set[str], Set[str]]:
        """(레거시) KOSPI200 + KOSDAQ150만 반환"""
        kospi200, _, kosdaq150, _ = StockMaster._sync_load_index_members()
        return kospi200, kosdaq150

    async def refresh_master(self) -> Dict[str, int]:
        """종목 마스터 전체 갱신"""
        await self._ensure_connected()

        logger.info("[StockMaster] 종목 마스터 갱신 시작...")

        # 동기 함수를 executor에서 실행
        loop = asyncio.get_event_loop()
        stocks = await loop.run_in_executor(_executor, self._sync_load_fdr)
        kospi200, kospi500, kosdaq150, market_caps = await loop.run_in_executor(
            _executor, self._sync_load_index_members
        )

        if not stocks:
            logger.error("[StockMaster] 종목 데이터 로드 실패")
            return {"total": 0}

        # 중복 제거 (ticker 기준)
        unique_stocks = {}
        for s in stocks:
            ticker = s["ticker"]
            if ticker and len(ticker) == 6 and ticker.isdigit():
                unique_stocks[ticker] = s

        # 지수 멤버십 + 시총 플래그 추가
        for ticker, stock in unique_stocks.items():
            stock["kospi200_yn"] = "Y" if ticker in kospi200 else "N"
            stock["kospi500_yn"] = "Y" if ticker in kospi500 else "N"
            stock["kosdaq150_yn"] = "Y" if ticker in kosdaq150 else "N"
            stock["market_cap"] = market_caps.get(ticker, 0)

        # DB에 저장 (UPSERT: 증분 갱신)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # UPSERT: 종목 정보 삽입 또는 업데이트
                    rows = [
                        (
                            s["ticker"],
                            s["corp_name"],
                            s["market"],
                            s["corp_cls"],
                            s["kospi200_yn"],
                            s["kosdaq150_yn"],
                            s["kospi500_yn"],
                            s["market_cap"],
                            datetime.now(),  # timezone-naive
                        )
                        for s in unique_stocks.values()
                    ]

                    await conn.executemany(
                        """
                        INSERT INTO kr_stock_master
                        (ticker, corp_name, market, corp_cls, kospi200_yn, kosdaq150_yn,
                         kospi500_yn, market_cap, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (ticker) DO UPDATE SET
                            corp_name = EXCLUDED.corp_name,
                            market = EXCLUDED.market,
                            corp_cls = EXCLUDED.corp_cls,
                            kospi200_yn = EXCLUDED.kospi200_yn,
                            kosdaq150_yn = EXCLUDED.kosdaq150_yn,
                            kospi500_yn = EXCLUDED.kospi500_yn,
                            market_cap = EXCLUDED.market_cap,
                            updated_at = EXCLUDED.updated_at
                        """,
                        rows,
                    )

            # 캐시 갱신
            await self._load_cache()

            # 통계
            stats = await self.get_stats()
            logger.info(f"[StockMaster] 갱신 완료: {stats}")

            return stats

        except asyncpg.PostgresError as e:
            logger.error(f"[StockMaster] DB 저장 실패: {e}")
            return {"total": 0, "error": str(e)}
        except asyncpg.InterfaceError as e:
            logger.error(f"[StockMaster] DB 연결 오류: {e}")
            return {"total": 0, "error": str(e)}

    async def lookup_ticker(self, name: str) -> Optional[str]:
        """종목명 → 코드 변환"""
        await self._ensure_connected()

        if not self._cache_loaded:
            await self._load_cache()

        # 1차: 정확 매칭
        if name in self._name_cache:
            return self._name_cache[name]

        # 2차: ILIKE 검색
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ticker FROM kr_stock_master WHERE corp_name ILIKE $1 LIMIT 1",
                name,
            )
            if row:
                return row["ticker"]

        return None

    async def validate_ticker(self, code: str) -> bool:
        """코드 검증"""
        await self._ensure_connected()

        if not self._cache_loaded:
            await self._load_cache()

        return code in self._ticker_set

    async def is_etf(self, code: str) -> bool:
        """
        종목코드가 ETF/ETN인지 DB 기반으로 판별

        market='ETF' 또는 corp_cls='ETF'인 경우 True 반환.
        캐시(_etf_set)가 로드되지 않았으면 DB에서 직접 조회.

        Args:
            code: 6자리 종목코드

        Returns:
            ETF/ETN 여부 (True=ETF/ETN, False=일반 주식 또는 미확인)
        """
        await self._ensure_connected()

        if not self._cache_loaded:
            await self._load_cache()

        return code in self._etf_set

    async def validate_tickers_batch(self, codes: List[str]) -> Set[str]:
        """코드 배치 검증

        Args:
            codes: 검증할 종목코드 리스트

        Returns:
            유효한 종목코드 집합
        """
        await self._ensure_connected()

        if not self._cache_loaded:
            await self._load_cache()

        return {code for code in codes if code in self._ticker_set}

    async def lookup_tickers_batch(self, names: List[str]) -> Dict[str, str]:
        """종목명 배치 변환

        Args:
            names: 종목명 리스트

        Returns:
            {종목명: 종목코드} 딕셔너리
        """
        await self._ensure_connected()

        if not self._cache_loaded:
            await self._load_cache()

        result = {}
        not_found = []

        # 1차: 캐시에서 정확 매칭
        for name in names:
            if name in self._name_cache:
                result[name] = self._name_cache[name]
            else:
                not_found.append(name)

        # 2차: ILIKE 검색 (캐시 미스만)
        if not_found:
            async with self.pool.acquire() as conn:
                for name in not_found:
                    row = await conn.fetchrow(
                        "SELECT ticker FROM kr_stock_master WHERE corp_name ILIKE $1 LIMIT 1",
                        name,
                    )
                    if row:
                        result[name] = row["ticker"]

        return result

    async def get_top_stocks(self, limit: int = 80) -> List[str]:
        """KOSPI500 + KOSDAQ150 종목 (LLM 힌트용, 시총 내림차순)"""
        await self._ensure_connected()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ticker, corp_name
                FROM kr_stock_master
                WHERE kospi500_yn = 'Y' OR kosdaq150_yn = 'Y'
                ORDER BY
                    CASE WHEN kospi200_yn = 'Y' THEN 0
                         WHEN kospi500_yn = 'Y' THEN 1
                         ELSE 2 END,
                    market_cap DESC,
                    ticker
                LIMIT $1
                """,
                limit,
            )

            return [f"{row['corp_name']}={row['ticker']}" for row in rows]

    async def get_tradeable_universe(
        self,
        kosdaq_top_n: int = 200,
        kosdaq_min_cap: int = 500,   # 억원
    ) -> Set[str]:
        """
        매매 가능 유니버스 반환 (스크리너 필터용)

        구성:
            - KOSPI500 구성 종목 전체
            - KOSDAQ150 구성 종목 전체
            - KOSDAQ 시총 상위 N개 (kosdaq_top_n, 기본 200개)
              단, 최소 시총 kosdaq_min_cap 억원 이상

        Returns:
            유효 종목코드 집합 (set of str)
        """
        await self._ensure_connected()

        async with self.pool.acquire() as conn:
            # KOSPI500 + KOSDAQ150
            rows = await conn.fetch(
                "SELECT ticker FROM kr_stock_master WHERE kospi500_yn = 'Y' OR kosdaq150_yn = 'Y'"
            )
            universe = {row["ticker"] for row in rows}

            # KOSDAQ 시총 상위 N개 추가
            kosdaq_rows = await conn.fetch(
                """
                SELECT ticker FROM kr_stock_master
                WHERE market = 'KOSDAQ'
                  AND market_cap >= $1
                ORDER BY market_cap DESC
                LIMIT $2
                """,
                kosdaq_min_cap,
                kosdaq_top_n,
            )
            for row in kosdaq_rows:
                universe.add(row["ticker"])

        return universe

    async def get_stats(self) -> Dict[str, int]:
        """종목 통계"""
        await self._ensure_connected()

        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM kr_stock_master")
            kospi = await conn.fetchval(
                "SELECT COUNT(*) FROM kr_stock_master WHERE market = 'KOSPI'"
            )
            kosdaq = await conn.fetchval(
                "SELECT COUNT(*) FROM kr_stock_master WHERE market = 'KOSDAQ'"
            )
            etf = await conn.fetchval(
                "SELECT COUNT(*) FROM kr_stock_master WHERE market = 'ETF'"
            )
            kospi200 = await conn.fetchval(
                "SELECT COUNT(*) FROM kr_stock_master WHERE kospi200_yn = 'Y'"
            )
            kospi500 = await conn.fetchval(
                "SELECT COUNT(*) FROM kr_stock_master WHERE kospi500_yn = 'Y'"
            )
            kosdaq150 = await conn.fetchval(
                "SELECT COUNT(*) FROM kr_stock_master WHERE kosdaq150_yn = 'Y'"
            )

            return {
                "total": total,
                "KOSPI": kospi,
                "KOSDAQ": kosdaq,
                "ETF": etf,
                "KOSPI200": kospi200,
                "KOSPI500": kospi500,
                "KOSDAQ150": kosdaq150,
            }


def get_stock_master(db_url: Optional[str] = None) -> StockMaster:
    """전역 싱글톤"""
    global _stock_master_instance

    if _stock_master_instance is None:
        if db_url is None:
            import os
            db_url = os.getenv("DATABASE_URL", "")
            if not db_url:
                raise ValueError("DATABASE_URL 환경변수 또는 db_url 파라미터 필요")
        _stock_master_instance = StockMaster(db_url)

    return _stock_master_instance
