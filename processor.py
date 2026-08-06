import asyncio
import glob
import json
import os
import shutil
import tempfile
from typing import Awaitable, Callable

ProgressCallback = Callable[[float], Awaitable[None]]


def _find_ffmpeg_binary(name: str) -> str:
    """ffmpeg/ffprobe ni PATH dan topadi; topilmasa winget o'rnatilgan joydan qidiradi."""
    found = shutil.which(name)
    if found:
        return found
    # winget orqali o'rnatilgan ffmpeg yo'li (Windows)
    winget_pattern = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "bin", f"{name}.exe",
    )
    matches = glob.glob(winget_pattern, recursive=True)
    if matches:
        return matches[0]
    return name  # Topilmasa oddiy nomini qaytaradi (PATH ga ishonadi)


FFPROBE = _find_ffmpeg_binary("ffprobe")
FFMPEG = _find_ffmpeg_binary("ffmpeg")

# Telegramning aylana video-note (video-xabar) uchun rasmiy hajm chegarasi (aynan 12 MB)
TELEGRAM_VIDEO_NOTE_MAX_BYTES = 12_582_912
# Xavfsizlik zaxirasi — chegaraga tegib ketmaslik uchun (konteyner/sarlavhalardagi kichik farqlar)
SIZE_SAFETY_MARGIN = 0.92
MIN_VIDEO_BITRATE_BPS = 250_000
MIN_AUDIO_BITRATE_BPS = 64_000
MAX_AUDIO_BITRATE_BPS = 128_000


def _parse_ffmpeg_timestamp(value: str) -> float:
    """ffmpeg vaqt matnini (masalan '00:00:04.500000') soniyaga aylantiradi."""
    try:
        hours, minutes, seconds = value.strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0


async def get_duration(path: str) -> float:
    cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration",
           "-of", "json", path]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe xatoligi: {err.decode(errors='ignore')[-300:]}")
    return float(json.loads(out)["format"]["duration"])


async def extract_embedded_cover(audio_path: str, out_image_path: str) -> bool:
    """Audio fayl ichidagi ichki albom rasmini (embedded cover art/ID3 tag) ffmpeg orqali ajratib oladi."""
    if not os.path.exists(audio_path):
        return False

    cmd_jpg = [
        FFMPEG, "-y",
        "-i", audio_path,
        "-an",
        "-vcodec", "mjpeg",
        "-ss", "0",
        "-frames:v", "1",
        out_image_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_jpg, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        if proc.returncode == 0 and os.path.exists(out_image_path) and os.path.getsize(out_image_path) > 0:
            return True
    except Exception:
        pass

    cmd_png = [
        FFMPEG, "-y",
        "-i", audio_path,
        "-an",
        "-vcodec", "png",
        "-ss", "0",
        "-frames:v", "1",
        out_image_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_png, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        if proc.returncode == 0 and os.path.exists(out_image_path) and os.path.getsize(out_image_path) > 0:
            return True
    except Exception:
        pass

    return False


def compute_bitrate_budget(duration: float) -> tuple[int, int]:
    """Natijaviy hajm Telegram chegarasidan oshmasligi uchun video/audio bitreytni (bps) hisoblaydi."""
    duration = max(duration, 1.0)
    target_total_bits = TELEGRAM_VIDEO_NOTE_MAX_BYTES * 8 * SIZE_SAFETY_MARGIN
    target_total_bps = target_total_bits / duration

    audio_bps = min(MAX_AUDIO_BITRATE_BPS, max(MIN_AUDIO_BITRATE_BPS, int(target_total_bps * 0.15)))
    video_bps = int(target_total_bps - audio_bps)

    if video_bps < MIN_VIDEO_BITRATE_BPS:
        video_bps = MIN_VIDEO_BITRATE_BPS
        audio_bps = MIN_AUDIO_BITRATE_BPS

    return video_bps, audio_bps
async def render_vinyl(disc_path: str, shadow_path: str, audio_path: str,
                        out_path: str, rotation_seconds: float | None = 4,
                        size: int = 640, fps: int = 30,
                        max_duration: float = 60.0,
                        start_offset: float = 0.0,
                        on_progress: ProgressCallback | None = None) -> str:
    duration = await get_duration(audio_path)

    # start_offset bo'lsa, mavjud davomiylikni moslashtirish
    if start_offset > 0:
        duration = max(0, duration - start_offset)

    duration = min(duration, max_duration)  # aylana video-note uchun Telegram chegarasi

    if rotation_seconds is None or rotation_seconds <= 0:
        rotation_seconds = 4.0

    video_bps, audio_bps = compute_bitrate_budget(duration)

    trimmed_audio_path = tempfile.mktemp(suffix=".trim.mp3")
    loop_path = tempfile.mktemp(suffix=".loop.mp4")

    try:
        # 1. Audio trim & conversion
        trim_cmd = [FFMPEG, "-y"]
        if start_offset > 0:
            trim_cmd.extend(["-ss", str(start_offset)])
        trim_cmd.extend([
            "-i", audio_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-threads", "0",
            "-t", str(duration),
            trimmed_audio_path,
        ])
        trim_proc = await asyncio.create_subprocess_exec(
            *trim_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, trim_err = await trim_proc.communicate()
        if trim_proc.returncode != 0:
            raise RuntimeError(f"ffmpeg audio qisqartirish xatoligi: {trim_err.decode(errors='ignore')[-500:]}")

        # 2. Render 1 seamless rotation loop (fast 4-second loop render)
        loop_dur = min(rotation_seconds, duration)
        filt_loop = (
            f"[0:v]format=rgba,rotate=2*PI*t/{rotation_seconds}:c=none:ow={size}:oh={size}[spin];"
            f"[spin][1:v]overlay=0:0:format=auto[vout]"
        )
        cmd_loop = [
            FFMPEG, "-y",
            "-loop", "1", "-i", disc_path,
            "-loop", "1", "-i", shadow_path,
            "-filter_complex", filt_loop,
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-threads", "0",
            "-b:v", str(video_bps), "-maxrate", str(int(video_bps * 1.15)),
            "-bufsize", str(video_bps * 2),
            "-t", str(loop_dur),
            "-r", str(fps),
            "-pix_fmt", "yuv420p",
            loop_path,
        ]
        loop_proc = await asyncio.create_subprocess_exec(
            *cmd_loop, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        await loop_proc.communicate()

        # 3. Stream loop video with audio (ultra-fast stream copy)
        if loop_proc.returncode == 0 and os.path.exists(loop_path) and os.path.getsize(loop_path) > 0:
            cmd_final = [
                FFMPEG, "-y",
                "-stream_loop", "-1", "-i", loop_path,
                "-i", trimmed_audio_path,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", str(audio_bps),
                "-t", str(duration),
                "-map", "0:v", "-map", "1:a",
                "-movflags", "+faststart",
                "-threads", "0",
                "-progress", "pipe:1", "-nostats",
                out_path,
            ]
        else:
            filt = (
                f"[1:v]format=rgba,rotate=2*PI*t/{rotation_seconds}:c=none:ow={size}:oh={size}[spin];"
                f"[spin][2:v]overlay=0:0:format=auto[vout]"
            )
            cmd_final = [
                FFMPEG, "-y",
                "-i", trimmed_audio_path,
                "-loop", "1", "-i", disc_path,
                "-loop", "1", "-i", shadow_path,
                "-filter_complex", filt,
                "-map", "[vout]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-threads", "0",
                "-b:v", str(video_bps), "-maxrate", str(int(video_bps * 1.15)),
                "-bufsize", str(video_bps * 2),
                "-c:a", "aac", "-b:a", str(audio_bps),
                "-t", str(duration),
                "-r", str(fps),
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats",
                out_path,
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd_final, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        async def _read_progress() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if not on_progress:
                    continue
                if text.startswith("out_time="):
                    elapsed = _parse_ffmpeg_timestamp(text.split("=", 1)[1])
                    if duration > 0:
                        percent = max(0.0, min(99.0, (elapsed / duration) * 100))
                        try:
                            await on_progress(percent)
                        except Exception:
                            pass
                elif text == "progress=end":
                    try:
                        await on_progress(100.0)
                    except Exception:
                        pass

        await asyncio.gather(_drain_stderr(), _read_progress())
        returncode = await proc.wait()
        if returncode != 0:
            raise RuntimeError(f"ffmpeg xatoligi: {b''.join(stderr_chunks).decode(errors='ignore')[-500:]}")

    finally:
        if os.path.exists(trimmed_audio_path):
            try:
                os.remove(trimmed_audio_path)
            except OSError:
                pass
        if os.path.exists(loop_path):
            try:
                os.remove(loop_path)
            except OSError:
                pass

    actual_size = os.path.getsize(out_path)
    if actual_size > TELEGRAM_VIDEO_NOTE_MAX_BYTES:
        raise RuntimeError(
            f"Natijaviy video hajmi ({actual_size} bayt) bitreyt sozlashiga qaramay "
            f"Telegram chegarasidan ({TELEGRAM_VIDEO_NOTE_MAX_BYTES} bayt) katta chiqdi."
        )

    return out_path
