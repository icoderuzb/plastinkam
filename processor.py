import asyncio
import glob
import json
import logging
import os
import shutil
import tempfile
from typing import Awaitable, Callable, List

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], Awaitable[None]]


def _find_ffmpeg_binary(name: str) -> str:
    """ffmpeg/ffprobe ni PATH dan topadi; topilmasa winget o'rnatilgan joydan qidiradi."""
    found = shutil.which(name)
    if found:
        return found
    winget_pattern = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "bin", f"{name}.exe",
    )
    matches = glob.glob(winget_pattern, recursive=True)
    if matches:
        return matches[0]
    return name


FFPROBE = _find_ffmpeg_binary("ffprobe")
FFMPEG = _find_ffmpeg_binary("ffmpeg")

# Telegramning video-note (dumaloq video-xabar) uchun Bot API rasmiy hajm chegarasi (20 MB)
TELEGRAM_VIDEO_NOTE_MAX_BYTES = 20_971_520
# Video tez yuklanishi va tejamkor bo'lishi uchun mo'ljallangan maqsadli hajm (~11-12 MB)
TARGET_VIDEO_NOTE_MAX_BYTES = 12_582_912
SIZE_SAFETY_MARGIN = 0.90
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
    target_total_bits = TARGET_VIDEO_NOTE_MAX_BYTES * 8 * SIZE_SAFETY_MARGIN
    target_total_bps = target_total_bits / duration

    audio_bps = min(MAX_AUDIO_BITRATE_BPS, max(MIN_AUDIO_BITRATE_BPS, int(target_total_bps * 0.15)))
    video_bps = int(target_total_bps - audio_bps)

    if video_bps < MIN_VIDEO_BITRATE_BPS:
        video_bps = MIN_VIDEO_BITRATE_BPS
        audio_bps = MIN_AUDIO_BITRATE_BPS

    return video_bps, audio_bps


async def concat_audio_files(
    audio_paths: List[str],
    out_path: str,
    max_duration: float = 60.0,
) -> str:
    """Bir nechta audio fayllarni bitta audio faylga birlashtiradi (Playlist/Album merge rejimi)."""
    if not audio_paths:
        raise ValueError("Birlashtirish uchun audio fayllar berilmadi.")

    if len(audio_paths) == 1:
        # Faqat 1 ta fayl bo'lsa, to'g'ridan-to'g'ri qisqartirib nusxalash
        cmd = [
            FFMPEG, "-y",
            "-i", audio_paths[0],
            "-t", str(max_duration),
            "-acodec", "libmp3lame",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Audio birlashtirish xatoligi: {err.decode(errors='ignore')[-300:]}")
        return out_path

    # Bir nechta audiolarni filter_complex orqali birlashtirish
    cmd = [FFMPEG, "-y"]
    filter_inputs = ""
    for i, path in enumerate(audio_paths):
        cmd.extend(["-i", path])
        filter_inputs += f"[{i}:a]"

    filter_complex = f"{filter_inputs}concat=n={len(audio_paths)}:v=0:a=1[outa]"
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[outa]",
        "-acodec", "libmp3lame",
        "-t", str(max_duration),
        out_path,
    ])

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Audio birlashtirish xatoligi: {err.decode(errors='ignore')[-400:]}")

    return out_path


def _is_deterministic_ffmpeg_error(err_str: str) -> bool:
    """Xatolik aniq va qayta urinishdan foyda yo'q ekanligini tekshiradi (invalid format, corrupt file va h.k.)."""
    e = err_str.lower()
    deterministic_keywords = [
        "invalid data found when processing input",
        "unknown format",
        "codec not supported",
        "no such file or directory",
        "moov atom not found",
        "invalid argument",
        "unspecified pixel format",
        "telegram chegarasidan",
    ]
    return any(kw in e for kw in deterministic_keywords)


async def _render_vinyl_once(
    disc_path: str,
    shadow_path: str,
    audio_path: str,
    out_path: str,
    rotation_seconds: float | None = 4,
    size: int = 640,
    fps: int = 30,
    max_duration: float = 60.0,
    start_offset: float = 0.0,
    on_progress: ProgressCallback | None = None,
) -> str:
    duration = await get_duration(audio_path)

    if start_offset > 0:
        duration = max(0, duration - start_offset)

    duration = min(duration, max_duration)

    if rotation_seconds is None or rotation_seconds <= 0:
        rotation_seconds = duration if duration > 0 else 4.0

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

        # 2. Render 1 seamless rotation loop
        # Agar aylanish vaqti qisqa bo'lsa (masalan 33 RPM: 1.8s, 45 RPM: 1.33s),
        # har bir aylanishda I-frame takrorlanib umumiy hajm shishib ketmasligi uchun
        # kamida 3.5 - 5 soniya atrofidagi to'liq aylanishlar miqdorini olamiz
        if rotation_seconds < duration:
            num_rotations = max(1, int(round(4.0 / rotation_seconds)))
            loop_dur = min(rotation_seconds * num_rotations, duration)
        else:
            loop_dur = duration

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
            "-c:v", "libx264", "-preset", "veryfast",
            "-threads", "0",
            "-b:v", str(video_bps), "-maxrate", str(video_bps),
            "-bufsize", str(video_bps),
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
                "-c:v", "libx264", "-preset", "veryfast",
                "-threads", "0",
                "-b:v", str(video_bps), "-maxrate", str(video_bps),
                "-bufsize", str(video_bps),
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
        logger.warning(
            "Natijaviy video hajmi (%d bayt) Telegram chegarasidan (%d bayt) oshdi. Qayta siqilmoqda...",
            actual_size, TELEGRAM_VIDEO_NOTE_MAX_BYTES
        )
        compressed_path = tempfile.mktemp(suffix=".comp.mp4")
        try:
            comp_cmd = [
                FFMPEG, "-y",
                "-i", out_path,
                "-c:v", "libx264", "-crf", "30", "-preset", "veryfast",
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-threads", "0",
                compressed_path,
            ]
            proc_comp = await asyncio.create_subprocess_exec(
                *comp_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc_comp.communicate()
            if proc_comp.returncode == 0 and os.path.exists(compressed_path):
                comp_size = os.path.getsize(compressed_path)
                if comp_size < actual_size:
                    shutil.move(compressed_path, out_path)
                    actual_size = comp_size
        finally:
            if os.path.exists(compressed_path):
                try:
                    os.remove(compressed_path)
                except OSError:
                    pass

    if actual_size > TELEGRAM_VIDEO_NOTE_MAX_BYTES:
        raise RuntimeError(
            f"Natijaviy video hajmi ({actual_size} bayt) bitreyt sozlashiga qaramay "
            f"Telegram chegarasidan ({TELEGRAM_VIDEO_NOTE_MAX_BYTES} bayt) katta chiqdi."
        )

    return out_path


async def render_vinyl(
    disc_path: str,
    shadow_path: str,
    audio_path: str,
    out_path: str,
    rotation_seconds: float | None = 4,
    size: int = 640,
    fps: int = 30,
    max_duration: float = 60.0,
    start_offset: float = 0.0,
    on_progress: ProgressCallback | None = None,
    max_retries: int = 2,
) -> str:
    """FFmpeg render jarayonini vaqtinchalik xatolar uchun qayta urinish (Retry) bilan bajaradi."""
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass

        try:
            return await _render_vinyl_once(
                disc_path=disc_path,
                shadow_path=shadow_path,
                audio_path=audio_path,
                out_path=out_path,
                rotation_seconds=rotation_seconds,
                size=size,
                fps=fps,
                max_duration=max_duration,
                start_offset=start_offset,
                on_progress=on_progress,
            )
        except Exception as exc:
            last_error = exc
            err_msg = str(exc)
            logger.warning(
                "FFmpeg render urinishi %s/%s muvaffaqiyatsiz bo'ldi: %s",
                attempt + 1, max_retries + 1, err_msg
            )

            # Agar xatolik deterministik bo'lsa (fayl buzilgan, noto'g'ri format), behuda qayta urinmaymiz
            if _is_deterministic_ffmpeg_error(err_msg):
                logger.info("Deterministik FFmpeg xatoligi aniqlandi, qayta urinish to'xtatildi.")
                raise exc

            if attempt < max_retries:
                backoff_time = 1.5 * (attempt + 1)
                await asyncio.sleep(backoff_time)

    if last_error:
        raise last_error
    raise RuntimeError("Noma'lum render xatoligi")
