"""
AI Trading Bot v2 - 종목 마스터 DB

한국 주식시장 전 종목(KOSPI, KOSDAQ, KONEX, ETF) 마스터 테이블을 관리합니다.
KOSPI200 / KOSDAQ150 지수 구성 종목 플래그도 포함합니다.

데이터 소스:
- FinanceDataReader (FDR): 전 종목 리스트
- pykrx: KOSPI200/KOSDAQ150 구성 종목 (FDR 폴백)
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
import asyncpg
from loguru import logger


# ============================================================
# SQL
# ============================================================

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS kr_stock_master (
    ticker VARCHAR(10) PRIMARY KEY,
    corp_name VARCHAR(200) NOT NULL,
    market VARCHAR(20) NOT NULL,
    kospi200_yn VARCHAR(1) DEFAULT 'N',
    kosdaq150_yn VARCHAR(1) DEFAULT 'N',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class StockMaster:
    """
    한국 주식시장 종목 마스터

    asyncpg Pool 패턴으로 PostgreSQL 연결을 관리하며,
    FDR/pykrx에서 종목 데이터를 로드하여 DB를 갱신합니다.
    """

    def __init__(self, database_url: Optional[str] = None):
        self._database_url = database_url or os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/ai_db"
        )
        self._pool: Optional[asyncpg.Pool] = None
        self._connect_lock = asyncio.Lock()

        # 인메모리 캐시
        self._name_cache: Dict[str, str] = {}   # 이름 → 코드
        self._ticker_set: Set[str] = set()       # 유효 코드 집합
        self._cache_updated_at: Optional[datetime] = None  # 캐시 갱신 시각
        self._cache_ttl_hours = 24  # 캐시 TTL (24시간)

    # ============================================================
    # 연결 관리
    # ============================================================

    async def connect(self) -> bool:
        """DB 연결"""
        try:
            if self._pool:
                try:
                    await self._pool.close()
                except Exception:
                    pass
                self._pool = None

            self._pool = await asyncpg.create_pool(
                self._database_url,
                min_size=1,
                max_size=3,
                command_timeout=60,
            )
            await self._ensure_table()
            logger.info("[StockMaster] PostgreSQL 연결 완료")
            return True
        except Exception as e:
            logger.error(f"[StockMaster] DB 연결 실패: {e}")
            return False

    async def disconnect(self):
        """DB 연결 해제"""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("[StockMaster] PostgreSQL 연결 해제")

    async def _ensure_connected(self):
        """연결 보장"""
        if self._pool:
            return
        async with self._connect_lock:
            if not self._pool:
                await self.connect()

    async def _ensure_table(self):
        """테이블 존재 보장 (스키마 변경 시 마이그레이션)"""
        async with self._pool.acquire() as conn:
            # 테이블이 존재하는 경우 필수 컬럼 확인
            exists = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'kr_stock_master'
                )
            """)
            if exists:
                # kospi200_yn 컬럼 존재 여부 확인
                has_col = await conn.fetchval("""
                    SELECT EXISTS(
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'kr_stock_master'
                          AND column_name = 'kospi200_yn'
                    )
                """)
                if not has_col:
                    # 스키마 변경: ALTER TABLE로 컬럼 추가 시도 (데이터 보존)
                    logger.info("[StockMaster] 스키마 변경 감지 → 컬럼 추가 시도")
                    try:
                        # kospi200_yn, kosdaq150_yn 컬럼 추가
                        await conn.execute("""
                            ALTER TABLE kr_stock_master
                            ADD COLUMN IF NOT EXISTS kospi200_yn VARCHAR(1) DEFAULT 'N'
                        """)
                        await conn.execute("""
                            ALTER TABLE kr_stock_master
                            ADD COLUMN IF NOT EXISTS kosdaq150_yn VARCHAR(1) DEFAULT 'N'
                        """)
                        logger.info("[StockMaster] 컬럼 추가 완료 (기존 데이터 보존)")
                    except Exception as e:
                        # ALTER 실패 시에만 테이블 재생성
                        logger.warning(f"[StockMaster] ALTER 실패 → 테이블 재생성: {e}")
                        await conn.execute("DROP TABLE kr_stock_master")
                        await conn.execute(_CREATE_TABLE_SQL)
                    return

            await conn.execute(_CREATE_TABLE_SQL)

    # ============================================================
    # 마스터 갱신
    # ============================================================

    async def refresh_master(self) -> Dict[str, int]:
        """
        종목 마스터 전체 갱신

        FDR/pykrx에서 동기 함수로 데이터를 로드한 뒤
        DB를 벌크 UPSERT로 교체하고, 상장폐지 종목을 삭제합니다.

        Returns:
            {"total": N, "kospi": N, "kosdaq": N, "konex": N, "etf": N,
             "kospi200": N, "kosdaq150": N, "deleted": N}
        """
        await self._ensure_connected()

        logger.info("[StockMaster] 종목 마스터 갱신 시작...")

        loop = asyncio.get_running_loop()

        # 1. FDR에서 전 종목 로드 (동기 → executor)
        all_stocks = await loop.run_in_executor(None, self._sync_load_fdr)
        if not all_stocks:
            logger.error("[StockMaster] FDR 종목 로드 실패")
            return {}

        # 2. KOSPI200/KOSDAQ150 로드 (동기 → executor)
        kospi200, kosdaq150 = await loop.run_in_executor(
            None, self._sync_load_kospi200_kosdaq150
        )

        kospi200_set = set(kospi200)
        kosdaq150_set = set(kosdaq150)

        # 3. 벌크 데이터 준비
        now = datetime.now()
        rows = []
        stats = {"total": 0, "kospi": 0, "kosdaq": 0, "konex": 0, "etf": 0,
                 "kospi200": len(kospi200_set), "kosdaq150": len(kosdaq150_set),
                 "deleted": 0}

        for ticker, name, market in all_stocks:
            k200 = "Y" if ticker in kospi200_set else "N"
            k150 = "Y" if ticker in kosdaq150_set else "N"
            rows.append((ticker, name, market, k200, k150, now))

            stats["total"] += 1
            market_lower = market.lower()
            if "kospi" in market_lower and "kosdaq" not in market_lower:
                stats["kospi"] += 1
            elif "kosdaq" in market_lower:
                stats["kosdaq"] += 1
            elif "konex" in market_lower:
                stats["konex"] += 1
            elif "etf" in market_lower:
                stats["etf"] += 1

        # 4. DB 벌크 UPSERT (임시 테이블 → MERGE) + 상장폐지 종목 삭제
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 임시 테이블 생성 (트랜잭션 종료 시 자동 삭제)
                await conn.execute("""
                    CREATE TEMP TABLE _tmp_stock_master (
                        ticker VARCHAR(10),
                        corp_name VARCHAR(200),
                        market VARCHAR(20),
                        kospi200_yn VARCHAR(1),
                        kosdaq150_yn VARCHAR(1),
                        updated_at TIMESTAMP
                    ) ON COMMIT DROP
                """)

                # 벌크 COPY로 임시 테이블에 삽입
                await conn.copy_records_to_table(
                    "_tmp_stock_master",
                    records=rows,
                    columns=["ticker", "corp_name", "market",
                             "kospi200_yn", "kosdaq150_yn", "updated_at"],
                )

                # 임시 테이블 → 본 테이블 UPSERT
                await conn.execute("""
                    INSERT INTO kr_stock_master
                        (ticker, corp_name, market, kospi200_yn, kosdaq150_yn, updated_at)
                    SELECT ticker, corp_name, market, kospi200_yn, kosdaq150_yn, updated_at
                    FROM _tmp_stock_master
                    ON CONFLICT (ticker) DO UPDATE SET
                        corp_name = EXCLUDED.corp_name,
                        market = EXCLUDED.market,
                        kospi200_yn = EXCLUDED.kospi200_yn,
                        kosdaq150_yn = EXCLUDED.kosdaq150_yn,
                        updated_at = EXCLUDED.updated_at
                """)

                # 상장폐지 종목 삭제 (새 데이터에 없는 기존 종목)
                # LEFT JOIN 방식으로 성능 최적화
                del_result = await conn.execute("""
                    DELETE FROM kr_stock_master m
                    USING (
                        SELECT m2.ticker
                        FROM kr_stock_master m2
                        LEFT JOIN _tmp_stock_master t ON m2.ticker = t.ticker
                        WHERE t.ticker IS NULL
                    ) AS to_delete
                    WHERE m.ticker = to_delete.ticker
                """)
                # del_result = "DELETE N" 형식
                try:
                    stats["deleted"] = int(del_result.split()[-1])
                except (ValueError, IndexError):
                    stats["deleted"] = 0

        # 5. 인메모리 캐시 갱신
        await self.rebuild_cache()

        logger.info(
            f"[StockMaster] 갱신 완료: 전체={stats['total']}, "
            f"KOSPI={stats['kospi']}, KOSDAQ={stats['kosdaq']}, "
            f"KONEX={stats['konex']}, ETF={stats['etf']}, "
            f"KOSPI200={stats['kospi200']}, KOSDAQ150={stats['kosdaq150']}, "
            f"삭제={stats['deleted']}"
        )
        return stats

    async def rebuild_cache(self):
        """DB에서 인메모리 캐시 재구축"""
        await self._ensure_connected()

        self._name_cache.clear()
        self._ticker_set.clear()

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ticker, corp_name FROM kr_stock_master"
            )
            for row in rows:
                ticker = row["ticker"]
                name = row["corp_name"]
                self._ticker_set.add(ticker)
                self._name_cache[name] = ticker

        # 캐시 갱신 시각 기록
        self._cache_updated_at = datetime.now()

        logger.debug(
            f"[StockMaster] 캐시 구축: {len(self._ticker_set)}개 종목, "
            f"{len(self._name_cache)}개 이름 매핑"
        )

    async def _ensure_cache_valid(self):
        """캐시 유효성 체크 (TTL 초과 시 재구축)"""
        if not self._cache_updated_at:
            await self.rebuild_cache()
            return

        elapsed = (datetime.now() - self._cache_updated_at).total_seconds() / 3600
        if elapsed > self._cache_ttl_hours:
            logger.info(f"[StockMaster] 캐시 TTL 초과 ({elapsed:.1f}시간) → 재구축")
            await self.rebuild_cache()

    # ============================================================
    # 조회 메서드
    # ============================================================

    async def lookup_ticker(self, name: str) -> Optional[str]:
        """
        종목명으로 종목코드 조회

        1차: 인메모리 캐시 정확 매칭
        2차: DB ILIKE 폴백
        """
        # 캐시 유효성 체크
        await self._ensure_cache_valid()

        # 캐시 정확 매칭
        name = name.strip()
        cached = self._name_cache.get(name)
        if cached:
            return cached

        # DB ILIKE 폴백
        await self._ensure_connected()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT ticker FROM kr_stock_master WHERE corp_name ILIKE $1 ORDER BY LENGTH(corp_name), corp_name LIMIT 1",
                    f"%{name}%"
                )
                if row:
                    ticker = row["ticker"]
                    self._name_cache[name] = ticker  # 캐시 추가
                    return ticker
        except Exception as e:
            logger.warning(f"[StockMaster] lookup_ticker 오류: {e}")

        return None

    async def validate_ticker(self, code: str) -> bool:
        """종목코드 유효성 검증"""
        code = code.strip()
        if not code:
            return False

        # 캐시 유효성 체크
        await self._ensure_cache_valid()

        # 캐시 확인
        if self._ticker_set:
            return code in self._ticker_set

        # DB 확인 (캐시가 비어있는 경우)
        await self._ensure_connected()
        try:
            async with self._pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM kr_stock_master WHERE ticker = $1)",
                    code
                )
                return bool(exists)
        except Exception as e:
            logger.warning(f"[StockMaster] validate_ticker 오류: {e}")
            return False

    async def get_top_stocks(self, limit: int = 80) -> List[Tuple[str, str]]:
        """
        KOSPI200 + KOSDAQ150 종목 반환 (LLM 힌트용)

        Returns:
            [(종목명, 종목코드), ...]
        """
        await self._ensure_connected()
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT corp_name, ticker FROM kr_stock_master
                    WHERE kospi200_yn = 'Y' OR kosdaq150_yn = 'Y'
                    ORDER BY
                        CASE WHEN kospi200_yn = 'Y' THEN 0 ELSE 1 END,
                        corp_name
                    LIMIT $1
                """, limit)
                return [(row["corp_name"], row["ticker"]) for row in rows]
        except Exception as e:
            logger.warning(f"[StockMaster] get_top_stocks 오류: {e}")
            return []

    async def get_stats(self) -> Dict[str, int]:
        """전체 통계"""
        await self._ensure_connected()
        try:
            async with self._pool.acquire() as conn:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM kr_stock_master"
                ) or 0
                kospi = await conn.fetchval(
                    "SELECT COUNT(*) FROM kr_stock_master WHERE market ILIKE '%KOSPI%' AND market NOT ILIKE '%KOSDAQ%'"
                ) or 0
                kosdaq = await conn.fetchval(
                    "SELECT COUNT(*) FROM kr_stock_master WHERE market ILIKE '%KOSDAQ%'"
                ) or 0
                etf = await conn.fetchval(
                    "SELECT COUNT(*) FROM kr_stock_master WHERE market ILIKE '%ETF%'"
                ) or 0
                k200 = await conn.fetchval(
                    "SELECT COUNT(*) FROM kr_stock_master WHERE kospi200_yn = 'Y'"
                ) or 0
                k150 = await conn.fetchval(
                    "SELECT COUNT(*) FROM kr_stock_master WHERE kosdaq150_yn = 'Y'"
                ) or 0

                return {
                    "total": total,
                    "kospi": kospi,
                    "kosdaq": kosdaq,
                    "etf": etf,
                    "kospi200": k200,
                    "kosdaq150": k150,
                }
        except Exception as e:
            logger.error(f"[StockMaster] get_stats 오류: {e}")
            return {}

    async def is_empty(self) -> bool:
        """테이블이 비어있는지 확인"""
        await self._ensure_connected()
        try:
            async with self._pool.acquire() as conn:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM kr_stock_master"
                )
                return (count or 0) == 0
        except Exception:
            return True

    # ============================================================
    # 동기 데이터 로드 (executor에서 실행)
    # ============================================================

    @staticmethod
    def _sync_load_fdr() -> List[Tuple[str, str, str]]:
        """
        FDR에서 전 종목 로드 (동기)

        Returns:
            [(ticker, name, market), ...]
        """
        results = []
        try:
            import FinanceDataReader as fdr

            markets = [
                ("KOSPI", "KOSPI"),
                ("KOSDAQ", "KOSDAQ"),
                ("KONEX", "KONEX"),
                ("ETF/KR", "ETF"),
            ]

            for fdr_market, label in markets:
                try:
                    df = fdr.StockListing(fdr_market)
                    if df is None or df.empty:
                        logger.warning(f"[StockMaster] FDR {fdr_market} 빈 결과")
                        continue

                    # 컬럼명 정규화 (FDR 버전별 차이 대응)
                    col_map = {}
                    for col in df.columns:
                        col_lower = col.lower()
                        if col_lower in ("code", "symbol", "ticker", "종목코드"):
                            col_map["ticker"] = col
                        elif col_lower in ("name", "종목명", "corp_name"):
                            col_map["name"] = col

                    ticker_col = col_map.get("ticker")
                    name_col = col_map.get("name")

                    if not ticker_col or not name_col:
                        logger.warning(
                            f"[StockMaster] FDR {fdr_market} 컬럼 매핑 실패: {list(df.columns)}"
                        )
                        continue

                    for _, row in df.iterrows():
                        ticker = str(row[ticker_col]).strip()
                        name = str(row[name_col]).strip()
                        if ticker and name and len(ticker) <= 10:
                            results.append((ticker, name, label))

                except Exception as e:
                    logger.warning(f"[StockMaster] FDR {fdr_market} 로드 오류: {e}")

            logger.info(f"[StockMaster] FDR 로드 완료: {len(results)}개 종목")

        except ImportError:
            logger.error("[StockMaster] FinanceDataReader 미설치")
        except Exception as e:
            logger.error(f"[StockMaster] FDR 로드 오류: {e}")

        return results

    @staticmethod
    def _sync_load_kospi200_kosdaq150() -> Tuple[List[str], List[str]]:
        """
        KOSPI200 / KOSDAQ150 구성 종목 로드 (동기)

        1차: pykrx
        2차: FDR 폴백

        Returns:
            (kospi200_tickers, kosdaq150_tickers)
        """
        kospi200 = []
        kosdaq150 = []

        # 1차: pykrx
        try:
            from pykrx import stock as pykrx_stock
            from datetime import datetime as _dt

            today_str = _dt.now().strftime("%Y%m%d")

            try:
                k200_df = pykrx_stock.get_index_portfolio_deposit_file("1028", today_str)
                if k200_df is not None and len(k200_df) > 0:
                    kospi200 = list(k200_df)
                    logger.info(f"[StockMaster] pykrx KOSPI200: {len(kospi200)}개")
            except Exception as e:
                logger.warning(f"[StockMaster] pykrx KOSPI200 실패: {e}")

            try:
                k150_df = pykrx_stock.get_index_portfolio_deposit_file("2203", today_str)
                if k150_df is not None and len(k150_df) > 0:
                    kosdaq150 = list(k150_df)
                    logger.info(f"[StockMaster] pykrx KOSDAQ150: {len(kosdaq150)}개")
            except Exception as e:
                logger.warning(f"[StockMaster] pykrx KOSDAQ150 실패: {e}")

        except ImportError:
            logger.warning("[StockMaster] pykrx 미설치, FDR 폴백 시도")

        # 2차: FDR 폴백 (pykrx 실패 시) — 한 번만 로드하여 공유
        if not kospi200 or not kosdaq150:
            try:
                import FinanceDataReader as fdr
                df = fdr.StockListing("KRX-MARCAP")
                if df is not None and not df.empty:
                    col_map = {}
                    for col in df.columns:
                        cl = col.lower()
                        if cl in ("code", "symbol", "ticker", "종목코드"):
                            col_map["ticker"] = col
                        elif cl in ("marcap", "시가총액", "market_cap"):
                            col_map["marcap"] = col
                        elif cl in ("market", "시장구분"):
                            col_map["market"] = col

                    ticker_col = col_map.get("ticker")
                    marcap_col = col_map.get("marcap")
                    market_col = col_map.get("market")

                    if ticker_col and marcap_col:
                        if not kospi200:
                            if market_col:
                                kospi_df = df[df[market_col].str.contains("KOSPI", na=False) &
                                             ~df[market_col].str.contains("KOSDAQ", na=False)]
                            else:
                                kospi_df = df
                            kospi_sorted = kospi_df.nlargest(200, marcap_col)
                            kospi200 = [str(t).strip() for t in kospi_sorted[ticker_col].tolist()]
                            logger.warning(
                                f"[StockMaster] pykrx KOSPI200 실패 → FDR 시총 폴백: {len(kospi200)}개 "
                                f"(정확도 저하 가능, 실제 지수 구성과 다를 수 있음)"
                            )

                        if not kosdaq150 and market_col:
                            kosdaq_df = df[df[market_col].str.contains("KOSDAQ", na=False)]
                            kosdaq_sorted = kosdaq_df.nlargest(150, marcap_col)
                            kosdaq150 = [str(t).strip() for t in kosdaq_sorted[ticker_col].tolist()]
                            logger.warning(
                                f"[StockMaster] pykrx KOSDAQ150 실패 → FDR 시총 폴백: {len(kosdaq150)}개 "
                                f"(정확도 저하 가능, 실제 지수 구성과 다를 수 있음)"
                            )
            except Exception as e:
                logger.warning(f"[StockMaster] FDR KOSPI200/KOSDAQ150 폴백 실패: {e}")

        return kospi200, kosdaq150


# ============================================================
# 전역 싱글톤
# ============================================================

_stock_master: Optional[StockMaster] = None


def get_stock_master() -> StockMaster:
    """전역 종목 마스터 인스턴스"""
    global _stock_master
    if _stock_master is None:
        _stock_master = StockMaster()
    return _stock_master
