"""
AI Trading Bot v2 - 텔레그램 알림 유틸리티

텔레그램 봇을 통한 메시지 발송 기능을 제공합니다.
"""

import os
import asyncio
from typing import Optional, List
import aiohttp
from loguru import logger


class TelegramNotifier:
    """텔레그램 알림 발송기"""

    # 텔레그램 메시지 최대 길이
    MAX_MESSAGE_LENGTH = 4096

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        alert_chat_id: Optional[str] = None,
        report_chat_id: Optional[str] = None,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID")
        self.alert_chat_id = alert_chat_id or os.getenv("TELEGRAM_CHAT_ID_ALERT") or "1754899925"
        self.report_chat_id = report_chat_id or os.getenv("TELEGRAM_CHAT_ID_REPORT") or "-1003374679062"

        self._session: Optional[aiohttp.ClientSession] = None

        if not self.bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다")
        if not self.chat_id:
            logger.warning("TELEGRAM_CHAT_ID가 설정되지 않았습니다")

    @property
    def is_configured(self) -> bool:
        """텔레그램 설정 완료 여부"""
        return bool(self.bot_token and self.chat_id)

    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> bool:
        """
        텔레그램 메시지 발송

        Args:
            text: 메시지 내용
            parse_mode: 파싱 모드 (HTML, Markdown, MarkdownV2)
            disable_notification: 알림 음소거 여부

        Returns:
            발송 성공 여부
        """
        if not self.is_configured:
            logger.warning("텔레그램 설정이 완료되지 않았습니다")
            return False

        # 메시지가 너무 길면 분할
        if len(text) > self.MAX_MESSAGE_LENGTH:
            return await self._send_long_message(text, parse_mode, disable_notification)

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }

        try:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    logger.debug("텔레그램 메시지 발송 성공")
                    return True
                else:
                    data = await resp.json()
                    logger.error(f"텔레그램 발송 실패: {data}")
                    return False

        except Exception as e:
            logger.error(f"텔레그램 발송 오류: {e}")
            return False

    async def _send_long_message(
        self,
        text: str,
        parse_mode: str,
        disable_notification: bool,
    ) -> bool:
        """긴 메시지 분할 발송"""
        chunks = self._split_message(text)
        success = True

        for i, chunk in enumerate(chunks, 1):
            logger.debug(f"텔레그램 청크 발송 [{i}/{len(chunks)}]")
            if not await self.send_message(chunk, parse_mode, disable_notification):
                success = False
            await asyncio.sleep(0.5)  # Rate limit 방지

        return success

    def _split_message(self, text: str) -> List[str]:
        """메시지를 적절한 크기로 분할"""
        max_len = self.MAX_MESSAGE_LENGTH - 100
        chunks = []
        lines = text.split("\n")
        current_chunk = ""

        for line in lines:
            # 단일 라인이 max_len 초과 시 강제 분할 (무한 루프 방지)
            if len(line) > max_len:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                for i in range(0, len(line), max_len):
                    chunks.append(line[i:i + max_len])
                continue

            if len(current_chunk) + len(line) + 1 > max_len:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks

    def send_sync(self, text: str, **kwargs) -> bool:
        """동기 방식 메시지 발송"""
        return asyncio.run(self.send_message(text, **kwargs))

    async def send_alert(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> bool:
        """
        에러/경고 알림 발송 (alert_chat_id로 전송)

        Args:
            text: 알림 내용
            parse_mode: 파싱 모드
            disable_notification: 알림 음소거 여부

        Returns:
            발송 성공 여부
        """
        if not self.bot_token or not self.alert_chat_id:
            logger.warning("텔레그램 알림 설정이 완료되지 않았습니다")
            return False

        # 메시지가 너무 길면 분할
        if len(text) > self.MAX_MESSAGE_LENGTH:
            return await self._send_long_alert(text, parse_mode, disable_notification)

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.alert_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }

        try:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    logger.debug("텔레그램 알림 발송 성공")
                    return True
                else:
                    data = await resp.json()
                    logger.error(f"텔레그램 알림 발송 실패: {data}")
                    return False

        except Exception as e:
            logger.error(f"텔레그램 알림 발송 오류: {e}")
            return False

    async def _send_long_alert(
        self,
        text: str,
        parse_mode: str,
        disable_notification: bool,
    ) -> bool:
        """긴 알림 메시지 분할 발송"""
        chunks = self._split_message(text)
        success = True

        for i, chunk in enumerate(chunks, 1):
            logger.debug(f"텔레그램 알림 청크 발송 [{i}/{len(chunks)}]")
            if not await self.send_alert(chunk, parse_mode, disable_notification):
                success = False
            await asyncio.sleep(0.5)

        return success

    async def close(self):
        """세션 정리"""
        if self._session and not self._session.closed:
            await self._session.close()

    def send_alert_sync(self, text: str, **kwargs) -> bool:
        """동기 방식 알림 발송"""
        return asyncio.run(self.send_alert(text, **kwargs))

    async def send_report(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> bool:
        """
        레포트 발송 (report_chat_id로 전송)

        8시/17시 일일 레포트 전용 채널로 발송합니다.
        """
        if not self.bot_token or not self.report_chat_id:
            logger.warning("텔레그램 레포트 설정이 완료되지 않았습니다")
            return False

        if len(text) > self.MAX_MESSAGE_LENGTH:
            chunks = self._split_message(text)
            success = True
            for chunk in chunks:
                if not await self._send_to_chat(self.report_chat_id, chunk, parse_mode, disable_notification):
                    success = False
                await asyncio.sleep(0.5)
            return success

        return await self._send_to_chat(self.report_chat_id, text, parse_mode, disable_notification)

    async def _send_to_chat(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> bool:
        """특정 채팅방으로 메시지 발송"""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }

        try:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return True
                else:
                    data = await resp.json()
                    logger.error(f"텔레그램 발송 실패 (chat={chat_id}): {data}")
                    return False
        except Exception as e:
            logger.error(f"텔레그램 발송 오류: {e}")
            return False


# 싱글톤 인스턴스
_notifier: Optional[TelegramNotifier] = None


def get_telegram_notifier() -> TelegramNotifier:
    """텔레그램 알림기 인스턴스 반환"""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


async def send_telegram(text: str, **kwargs) -> bool:
    """텔레그램 메시지 발송 (편의 함수)"""
    return await get_telegram_notifier().send_message(text, **kwargs)


async def send_alert(text: str, **kwargs) -> bool:
    """에러/경고 알림 발송 (편의 함수)"""
    return await get_telegram_notifier().send_alert(text, **kwargs)
