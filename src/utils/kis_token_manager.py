"""
AI Trading Bot v2 - KIS API 토큰 매니저

KIS API의 OAuth 토큰 및 WebSocket Approval Key를
전역적으로 관리하고 여러 컴포넌트에서 재사용합니다.

토큰 종류:
1. Access Token (REST API용) - 24시간 유효
2. Approval Key (WebSocket용) - 세션당 유효

특징:
- 싱글톤 패턴으로 전역 인스턴스 제공
- 파일 캐싱으로 재시작 시에도 토큰 유지
- 자동 갱신으로 만료 전 토큰 갱신
- 멀티프로세스 환경에서도 토큰 공유 가능
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple
import aiohttp
from loguru import logger


class KISTokenManager:
    """
    KIS API 토큰 매니저

    싱글톤으로 전역에서 토큰을 관리합니다.
    """

    _instance: Optional["KISTokenManager"] = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 이미 초기화된 경우 스킵
        if hasattr(self, '_initialized') and self._initialized:
            return

        self._initialized = True

        # API 설정
        self._app_key = os.getenv("KIS_APPKEY", "") or os.getenv("KIS_APP_KEY", "")
        self._app_secret = os.getenv("KIS_APPSECRET", "") or os.getenv("KIS_SECRET_KEY", "")
        self._env = os.getenv("KIS_ENV", "prod")

        # 토큰
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._approval_key: Optional[str] = None
        self._approval_expires_at: Optional[datetime] = None

        # HTTP 세션 (lazy init)
        self._session: Optional[aiohttp.ClientSession] = None

        # 캐시 경로
        cache_dir = Path(os.path.expanduser("~/.cache/ai_trader"))
        cache_dir.mkdir(parents=True, exist_ok=True)

        self._token_cache_path = cache_dir / f"kis_token_{self._env}.json"
        self._approval_cache_path = cache_dir / f"kisauth_approval_{self._env}.json"

        # API URL
        if self._env == "prod":
            self._base_url = "https://openapi.koreainvestment.com:9443"
        else:
            self._base_url = "https://openapivts.koreainvestment.com:29443"

        # 캐시된 토큰 로드
        self._load_cached_tokens()

        logger.debug(f"KISTokenManager 초기화: env={self._env}")

    # ============================================================
    # 공개 인터페이스
    # ============================================================

    async def get_access_token(self) -> Optional[str]:
        """
        REST API용 Access Token 반환

        토큰이 없거나 만료 임박 시 자동 갱신합니다.
        """
        async with self._lock:
            # 토큰이 유효하면 반환
            if self._is_token_valid():
                return self._access_token

            # 토큰 발급
            success = await self._issue_access_token()
            return self._access_token if success else None

    async def get_approval_key(self) -> Optional[str]:
        """
        WebSocket용 Approval Key 반환

        Approval Key는 한 번 발급 후 세션 동안 유효합니다.
        (공식 문서상 유효기간이 명시되지 않아 6시간으로 설정)
        """
        async with self._lock:
            # 키가 유효하면 반환
            if self._is_approval_valid():
                return self._approval_key

            # 키 발급
            success = await self._issue_approval_key()
            return self._approval_key if success else None

    async def refresh_all(self) -> bool:
        """모든 토큰 강제 갱신"""
        async with self._lock:
            token_ok = await self._issue_access_token()
            approval_ok = await self._issue_approval_key()
            return token_ok and approval_ok

    def invalidate(self):
        """토큰 무효화 (재발급 필요 시)"""
        self._access_token = None
        self._token_expires_at = None
        self._approval_key = None
        self._approval_expires_at = None
        logger.info("KIS 토큰 무효화됨")

    @property
    def app_key(self) -> str:
        """App Key"""
        return self._app_key

    @property
    def app_secret(self) -> str:
        """App Secret"""
        return self._app_secret

    @property
    def env(self) -> str:
        """환경 (prod/dev)"""
        return self._env

    @property
    def base_url(self) -> str:
        """API Base URL"""
        return self._base_url

    @property
    def token_info(self) -> dict:
        """토큰 상태 정보"""
        return {
            "access_token_valid": self._is_token_valid(),
            "token_expires_at": self._token_expires_at.isoformat() if self._token_expires_at else None,
            "approval_key_valid": self._is_approval_valid(),
            "approval_expires_at": self._approval_expires_at.isoformat() if self._approval_expires_at else None,
            "env": self._env,
        }

    # ============================================================
    # 토큰 유효성 검사
    # ============================================================

    def _is_token_valid(self) -> bool:
        """Access Token 유효성 검사"""
        if not self._access_token or not self._token_expires_at:
            return False

        # 만료 5분 전이면 갱신 필요
        return datetime.now() < self._token_expires_at - timedelta(minutes=5)

    def _is_approval_valid(self) -> bool:
        """Approval Key 유효성 검사"""
        if not self._approval_key or not self._approval_expires_at:
            return False

        # 만료 5분 전이면 갱신 필요
        return datetime.now() < self._approval_expires_at - timedelta(minutes=5)

    # ============================================================
    # 토큰 발급
    # ============================================================

    async def _ensure_session(self):
        """HTTP 세션 보장"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def _issue_access_token(self) -> bool:
        """Access Token 발급"""
        try:
            await self._ensure_session()

            url = f"{self._base_url}/oauth2/tokenP"
            body = {
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            }

            async with self._session.post(url, json=body) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"[TokenManager] Access Token 발급 실패: {resp.status} - {text}")
                    return False

                data = await resp.json()

                self._access_token = data.get("access_token")
                expires_in = int(data.get("expires_in", 86400))  # 기본 24시간
                self._token_expires_at = datetime.now() + timedelta(seconds=expires_in)

                # 캐시 저장
                await self._save_token_cache()

                logger.info(
                    f"[TokenManager] Access Token 발급 완료 "
                    f"(만료: {self._token_expires_at.strftime('%Y-%m-%d %H:%M:%S')})"
                )
                return True

        except Exception as e:
            logger.exception(f"[TokenManager] Access Token 발급 오류: {e}")
            return False

    async def _issue_approval_key(self) -> bool:
        """WebSocket Approval Key 발급"""
        try:
            await self._ensure_session()

            url = f"{self._base_url}/oauth2/Approval"
            body = {
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "secretkey": self._app_secret,
            }

            async with self._session.post(url, json=body) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"[TokenManager] Approval Key 발급 실패: {resp.status} - {text}")
                    return False

                data = await resp.json()

                self._approval_key = data.get("approval_key")
                # Approval Key는 유효기간이 명시되지 않음, 6시간으로 설정
                self._approval_expires_at = datetime.now() + timedelta(hours=6)

                # 캐시 저장
                await self._save_approval_cache()

                logger.info(
                    f"[TokenManager] Approval Key 발급 완료 "
                    f"(만료: {self._approval_expires_at.strftime('%Y-%m-%d %H:%M:%S')})"
                )
                return True

        except Exception as e:
            logger.exception(f"[TokenManager] Approval Key 발급 오류: {e}")
            return False

    # ============================================================
    # 캐시 관리
    # ============================================================

    def _load_cached_tokens(self):
        """캐시된 토큰 로드"""
        # Access Token 로드
        try:
            if self._token_cache_path.exists():
                with open(self._token_cache_path, 'r') as f:
                    cache = json.load(f)

                expires_at_str = cache.get("expires_at")
                if expires_at_str:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    # 만료 5분 전까지만 유효
                    if datetime.now() < expires_at - timedelta(minutes=5):
                        self._access_token = cache.get("token")
                        self._token_expires_at = expires_at
                        logger.debug("[TokenManager] 캐시된 Access Token 로드 완료")
        except Exception as e:
            logger.debug(f"[TokenManager] Access Token 캐시 로드 실패: {e}")

        # Approval Key 로드
        try:
            if self._approval_cache_path.exists():
                with open(self._approval_cache_path, 'r') as f:
                    cache = json.load(f)

                expires_at_str = cache.get("expires_at")
                if expires_at_str:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if datetime.now() < expires_at - timedelta(minutes=5):
                        self._approval_key = cache.get("approval_key")
                        self._approval_expires_at = expires_at
                        logger.debug("[TokenManager] 캐시된 Approval Key 로드 완료")
        except Exception as e:
            logger.debug(f"[TokenManager] Approval Key 캐시 로드 실패: {e}")

    async def _save_token_cache(self):
        """Access Token 캐시 저장 (비동기)"""
        try:
            cache = {
                "token": self._access_token,
                "expires_at": self._token_expires_at.isoformat() if self._token_expires_at else None,
                "updated_at": datetime.now().isoformat(),
            }
            await asyncio.to_thread(self._write_json, self._token_cache_path, cache)
        except Exception as e:
            logger.debug(f"[TokenManager] Access Token 캐시 저장 실패: {e}")

    async def _save_approval_cache(self):
        """Approval Key 캐시 저장 (비동기)"""
        try:
            cache = {
                "approval_key": self._approval_key,
                "expires_at": self._approval_expires_at.isoformat() if self._approval_expires_at else None,
                "updated_at": datetime.now().isoformat(),
            }
            await asyncio.to_thread(self._write_json, self._approval_cache_path, cache)
        except Exception as e:
            logger.debug(f"[TokenManager] Approval Key 캐시 저장 실패: {e}")

    @staticmethod
    def _write_json(path, data):
        """JSON 파일 쓰기 (동기, to_thread에서 호출)"""
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    # ============================================================
    # 정리
    # ============================================================

    async def close(self):
        """리소스 정리"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


# ============================================================
# 전역 인스턴스 접근
# ============================================================

_token_manager: Optional[KISTokenManager] = None


def get_token_manager() -> KISTokenManager:
    """전역 토큰 매니저 인스턴스"""
    global _token_manager
    if _token_manager is None:
        _token_manager = KISTokenManager()
    return _token_manager


async def get_access_token() -> Optional[str]:
    """Access Token 획득 (편의 함수)"""
    return await get_token_manager().get_access_token()


async def get_approval_key() -> Optional[str]:
    """Approval Key 획득 (편의 함수)"""
    return await get_token_manager().get_approval_key()
