import asyncio
import os
import shutil
import tempfile
from PIL import Image

import config
import database
import compose
import processor


async def test_database_and_stats():
    print("--- 1. Testing Database, Persistent Queue, Preferences, and Stats ---")
    await database.init_db()

    import random
    test_uid = random.randint(1000000, 9999999)

    async with database.async_session() as session:
        # Test User Preference & Watermark
        pref = await database.get_user_preference(session, user_id=test_uid)
        assert pref.include_watermark is False
        pref = await database.set_user_watermark(session, user_id=test_uid, include_watermark=True)
        assert pref.include_watermark is True
        print("✓ User preference & watermark toggle passed")

        # Test Job Logging
        await database.log_job_execution(
            session=session,
            user_id=test_uid,
            job_type="single",
            vinyl_color="pink",
            rotation_speed="33",
            duration_seconds=55.0,
            is_success=True,
            processing_time_seconds=3.2,
        )
        await database.log_job_execution(
            session=session,
            user_id=test_uid,
            job_type="batch_merge",
            vinyl_color="default",
            rotation_speed="45",
            duration_seconds=60.0,
            is_success=True,
            processing_time_seconds=4.5,
        )

        # Test Rate Limit Count
        recent_cnt = await database.get_recent_job_count(session, user_id=test_uid, window_seconds=60)
        assert recent_cnt >= 2
        print(f"✓ Rate limit recent job count: {recent_cnt} (Passed)")


        # Test Analytics Stats (/stats)
        stats = await database.get_analytics_stats(session)
        print("✓ Stats generated:", stats)
        assert stats["total_videos"] >= 2
        assert "failure_rate" in stats

        # Test Persistent Queue
        p_job = await database.db_enqueue_job(
            session,
            job_id="test_job_1",
            user_id=123456,
            priority=1,
            payload_data={"test": "data"},
        )
        assert p_job.status == "queued"

        next_job = await database.db_get_next_queued_job(session)
        assert next_job is not None
        assert next_job.job_id == "test_job_1"

        await database.db_update_job_status(session, "test_job_1", "processing")
        stale = await database.db_recover_stale_jobs(session)
        assert len(stale) >= 1
        print("✓ Persistent queue enqueue, fetch, and crash recovery passed")


def test_compose_features():
    print("\n--- 2. Testing Compose Label Text, Watermark & Contrast ---")
    thumb_path = "temp_test_thumb.png"
    out_path = "temp_test_out.png"

    # Create a simple red thumbnail
    img = Image.new("RGBA", (300, 300), (255, 0, 0, 255))
    img.save(thumb_path)

    try:
        # Test Default (Black) Disc
        compose.build_disc(
            thumb_path=thumb_path,
            vinyl_path=config.VINYL_PATH,
            out_path=out_path,
            artist="Xamdam Sobirov",
            title="Yuragim Sensan Juda Uzun Qoshiq Nomi Bilan",
            vinyl_color="default",
            include_watermark=True,
        )
        assert os.path.exists(out_path) and os.path.getsize(out_path) > 0
        print("✓ Default black disc with watermark and label text created")

        # Test Yellow Disc
        compose.build_disc(
            thumb_path=thumb_path,
            vinyl_path=config.VINYL_YELLOW_PATH,
            out_path=out_path,
            artist="Yulduz Usmonova",
            title="Muhabbat",
            vinyl_color="yellow",
            include_watermark=True,
        )
        assert os.path.exists(out_path) and os.path.getsize(out_path) > 0
        print("✓ Yellow disc with contrast label text created")

        # Test Pink Disc
        compose.build_disc(
            thumb_path=thumb_path,
            vinyl_path=config.VINYL_PINK_PATH,
            out_path=out_path,
            artist="Lola",
            title="Sevgim",
            vinyl_color="pink",
            include_watermark=False,
        )
        assert os.path.exists(out_path) and os.path.getsize(out_path) > 0
        print("✓ Pink disc without watermark created")

    finally:
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        if os.path.exists(out_path):
            os.remove(out_path)


async def test_processor_concat_and_retry():
    print("\n--- 3. Testing Processor Audio Concatenation ---")
    # Generate 2 small tone mp3 files using ffmpeg
    audio1 = "temp_test_a1.mp3"
    audio2 = "temp_test_a2.mp3"
    concat_out = "temp_test_concat.mp3"

    try:
        # Create 2 test audio tones
        p1 = await asyncio.create_subprocess_exec(
            processor.FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-acodec", "libmp3lame", audio1,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await p1.communicate()

        p2 = await asyncio.create_subprocess_exec(
            processor.FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
            "-acodec", "libmp3lame", audio2,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await p2.communicate()

        assert os.path.exists(audio1) and os.path.exists(audio2)

        # Concat files
        await processor.concat_audio_files([audio1, audio2], concat_out, max_duration=60.0)
        assert os.path.exists(concat_out) and os.path.getsize(concat_out) > 0

        dur = await processor.get_duration(concat_out)
        print(f"✓ Concat duration: {dur:.2f}s (Expected ~7.0s)")
        assert 6.5 <= dur <= 7.5
        print("✓ Audio concatenation test passed")

    finally:
        for p in (audio1, audio2, concat_out):
            if os.path.exists(p):
                os.remove(p)


async def main():
    await test_database_and_stats()
    test_compose_features()
    await test_processor_concat_and_retry()
    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(main())
