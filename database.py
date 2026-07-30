import os
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config

DB_PATH = os.path.join(config.BASE_DIR, "vinylbot.db")
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class Channel(Base):
    """Majburiy kanallar modeli (mandatory_channels jadvali)."""

    __tablename__ = "mandatory_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)

    def __repr__(self) -> str:
        return f"<Channel(id={self.id}, name='{self.name}', url='{self.url}')>"


async def init_db() -> None:
    """Jadvallarni yaratish va ma'lumotlar bazasini ishga tushirish."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class DbSessionMiddleware(BaseMiddleware):
    """Har bir kelayotgan event (Message, CallbackQuery) ga AsyncSession ob'ektini data['session'] ga ulash."""

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        async with async_session() as session:
            data["session"] = session
            return await handler(event, data)
