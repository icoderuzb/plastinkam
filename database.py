import datetime
import json
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional
from aiogram import BaseMiddleware
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    desc,
    func,
    select,
    update,
    delete,
)
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


class UserPreference(Base):
    """Foydalanuvchi sozlamalari (watermark, tanlangan rang/tezlik)."""

    __tablename__ = "user_preferences"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    include_watermark: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_speed: Mapped[str] = mapped_column(String(32), default="33")
    preferred_vinyl: Mapped[str] = mapped_column(String(32), default="default")
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class JobLog(Base):
    """Bajarilgan va xatolikka uchragan so'rovlar jurnali (statistika uchun)."""

    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    job_type: Mapped[str] = mapped_column(String(32), default="single")  # single, batch_merge, batch_separate
    vinyl_color: Mapped[str] = mapped_column(String(32), default="default")
    rotation_speed: Mapped[str] = mapped_column(String(32), default="33")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    is_success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    processing_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=func.now(), index=True
    )


class PendingJob(Base):
    """Bazada saqlanuvchi bardoshli navbat (Persistent Queue)."""

    __tablename__ = "pending_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=1, index=True)  # 0 for Developer, 1 for regular
    status: Mapped[str] = mapped_column(
        String(32), default="queued", index=True
    )  # queued, processing, done, failed, canceled
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=func.now(), index=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


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


# ============================================================
# Helper Functions: User Preferences
# ============================================================

async def get_user_preference(session: AsyncSession, user_id: int) -> UserPreference:
    """Foydalanuvchi sozlamalarini olish yoki default qiymat bilan yaratish."""
    stmt = select(UserPreference).where(UserPreference.user_id == user_id)
    result = await session.execute(stmt)
    pref = result.scalar_one_or_none()
    if not pref:
        pref = UserPreference(user_id=user_id, include_watermark=False)
        session.add(pref)
        await session.commit()
    return pref


async def set_user_watermark(session: AsyncSession, user_id: int, include_watermark: bool) -> UserPreference:
    """Watermark tanlovini saqlash."""
    pref = await get_user_preference(session, user_id)
    pref.include_watermark = include_watermark
    await session.commit()
    return pref


# ============================================================
# Helper Functions: Job Logging & Rate Limiting & Stats
# ============================================================

async def log_job_execution(
    session: AsyncSession,
    user_id: int,
    job_type: str,
    vinyl_color: str,
    rotation_speed: str,
    duration_seconds: float,
    is_success: bool,
    processing_time_seconds: float,
    error_message: Optional[str] = None,
) -> JobLog:
    """Bajarilgan yoki xato bilan tugagan har bir vazifani DB ga qayd etish."""
    log_entry = JobLog(
        user_id=user_id,
        job_type=job_type,
        vinyl_color=vinyl_color,
        rotation_speed=rotation_speed,
        duration_seconds=duration_seconds,
        is_success=is_success,
        error_message=error_message[:500] if error_message else None,
        processing_time_seconds=processing_time_seconds,
    )
    session.add(log_entry)
    await session.commit()
    return log_entry


async def get_recent_job_count(session: AsyncSession, user_id: int, window_seconds: int) -> int:
    """So'nggi window_seconds ichida foydalanuvchi tomonidan yuborilgan so'rovlar soni (Rate Limiter uchun)."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=window_seconds)
    stmt = select(func.count(JobLog.id)).where(
        JobLog.user_id == user_id,
        JobLog.created_at >= cutoff,
    )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


async def get_analytics_stats(session: AsyncSession) -> Dict[str, Any]:
    """Admin /stats buyrug'i uchun umumiy va davriy statistikalarni hisoblash."""
    now = datetime.datetime.now(datetime.timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - datetime.timedelta(days=now.weekday())
    month_start = today_start.replace(day=1)

    # 1. Jami videolar soni (all-time)
    stmt_total = select(func.count(JobLog.id)).where(JobLog.is_success == True)
    total_videos = (await session.execute(stmt_total)).scalar_one() or 0

    # 2. Bugungi videolar
    stmt_today = select(func.count(JobLog.id)).where(
        JobLog.is_success == True, JobLog.created_at >= today_start
    )
    today_videos = (await session.execute(stmt_today)).scalar_one() or 0

    # 3. Shu haftadagi videolar
    stmt_week = select(func.count(JobLog.id)).where(
        JobLog.is_success == True, JobLog.created_at >= week_start
    )
    week_videos = (await session.execute(stmt_week)).scalar_one() or 0

    # 4. Shu oydagi videolar
    stmt_month = select(func.count(JobLog.id)).where(
        JobLog.is_success == True, JobLog.created_at >= month_start
    )
    month_videos = (await session.execute(stmt_month)).scalar_one() or 0

    # 5. Eng ko'p ishlatilgan rang (Top Color)
    stmt_color = (
        select(JobLog.vinyl_color, func.count(JobLog.id).label("cnt"))
        .where(JobLog.is_success == True)
        .group_by(JobLog.vinyl_color)
        .order_by(desc("cnt"))
        .limit(1)
    )
    top_color_row = (await session.execute(stmt_color)).first()
    top_color = f"{top_color_row[0]} ({top_color_row[1]})" if top_color_row else "Mavjud emas"

    # 6. Eng ko'p ishlatilgan tezlik (Top Speed)
    stmt_speed = (
        select(JobLog.rotation_speed, func.count(JobLog.id).label("cnt"))
        .where(JobLog.is_success == True)
        .group_by(JobLog.rotation_speed)
        .order_by(desc("cnt"))
        .limit(1)
    )
    top_speed_row = (await session.execute(stmt_speed)).first()
    top_speed = f"{top_speed_row[0]} ({top_speed_row[1]})" if top_speed_row else "Mavjud emas"

    # 7. So'nggi 50 ta so'rovdagi xatolik darajasi (Failure Rate)
    stmt_last50 = select(JobLog.is_success).order_by(desc(JobLog.id)).limit(50)
    last50_results = (await session.execute(stmt_last50)).scalars().all()
    if last50_results:
        failed_count = sum(1 for s in last50_results if not s)
        failure_rate = (failed_count / len(last50_results)) * 100
        failure_str = f"{failure_rate:.1f}% ({failed_count}/{len(last50_results)})"
    else:
        failure_str = "0% (0/0)"

    return {
        "total_videos": total_videos,
        "today_videos": today_videos,
        "week_videos": week_videos,
        "month_videos": month_videos,
        "top_color": top_color,
        "top_speed": top_speed,
        "failure_rate": failure_str,
    }


# ============================================================
# Helper Functions: Persistent Queue Management
# ============================================================

async def db_enqueue_job(
    session: AsyncSession,
    job_id: str,
    user_id: int,
    priority: int,
    payload_data: dict,
) -> PendingJob:
    """Yangi vazifani persistent navbatga (pending_jobs) qo'shish."""
    p_job = PendingJob(
        job_id=job_id,
        user_id=user_id,
        priority=priority,
        status="queued",
        payload=json.dumps(payload_data),
    )
    session.add(p_job)
    await session.commit()
    return p_job


async def db_update_job_status(
    session: AsyncSession,
    job_id: str,
    status: str,
) -> None:
    """Navbatdagi vazifa holatini yangilash (queued -> processing -> done / failed / canceled)."""
    stmt = (
        update(PendingJob)
        .where(PendingJob.job_id == job_id)
        .values(status=status, updated_at=func.now())
    )
    await session.execute(stmt)
    await session.commit()


async def db_get_next_queued_job(session: AsyncSession) -> Optional[PendingJob]:
    """Navbatdagi eng yuqori ustuvorlikdagi (priority=0 avval, so'ng vaqt bo'yicha) vazifani olish."""
    stmt = (
        select(PendingJob)
        .where(PendingJob.status == "queued")
        .order_by(PendingJob.priority.asc(), PendingJob.created_at.asc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def db_recover_stale_jobs(session: AsyncSession) -> List[PendingJob]:
    """Bot qayta ishga tushganda 'processing' holatida to'xtab qolgan vazifalarni 'queued' holatiga qaytarish."""
    stmt = select(PendingJob).where(PendingJob.status == "processing")
    result = await session.execute(stmt)
    stale_jobs = list(result.scalars().all())
    for job in stale_jobs:
        job.status = "queued"
    if stale_jobs:
        await session.commit()
    return stale_jobs
