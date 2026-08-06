import re
from typing import List
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Channel


def extract_chat_id(url_or_username: str) -> str:
    """URL yoki username dan Telegram chat_id / @username ajratib oladi.

    Misol:
      - 'https://t.me/my_channel' -> '@my_channel'
      - '@my_channel' -> '@my_channel'
    """
    url = url_or_username.strip()
    if url.startswith("@"):
        return url

    match = re.search(r"(?:https?://)?t\.me/([a-zA-Z0-9_]+)/?$", url)
    if match:
        username = match.group(1)
        if not username.startswith("+") and username != "joinchat":
            return f"@{username}"

    return url


async def check_subscriptions(bot: Bot, user_id: int, session: AsyncSession) -> List[Channel]:
    """Foydalanuvchi a'zo bo'lmagan majburiy kanallar ro'yxatini qaytaradi.

    Agar foydalanuvchi barcha kanallarga a'zo bo'lsa, bo'sh ro'yxat [] qaytadi.
    """
    stmt = select(Channel)
    result = await session.execute(stmt)
    channels: List[Channel] = list(result.scalars().all())

    unsubscribed_channels: List[Channel] = []

    for ch in channels:
        chat_identifier = extract_chat_id(ch.url)
        try:
            member = await bot.get_chat_member(chat_id=chat_identifier, user_id=user_id)
            if member.status in (
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
                ChatMemberStatus.RESTRICTED,
            ):
                unsubscribed_channels.append(ch)
        except (TelegramBadRequest, TelegramForbiddenError):
            # Bot kanalda admin emas yoki kanal ommaviy emas/topilmadi
            unsubscribed_channels.append(ch)
        except Exception:
            unsubscribed_channels.append(ch)

    return unsubscribed_channels
