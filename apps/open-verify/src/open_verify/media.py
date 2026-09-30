"""Bounded local MP4 encoding, independent of browser and agent providers."""

import asyncio
import os
import shutil
from pathlib import Path

MAX_VIDEO_BYTES = 10_000_000
TARGET_VIDEO_BYTES = 9_000_000


async def encode_video(source: Path, destination: Path) -> str | None:
    """Return an omission reason on failure; never return a partial/oversized clip."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "Video omitted: ffmpeg is not installed or is not on PATH."
    # A journey is limited to 120 seconds. Cap the encoder's rate conservatively
    # for that full duration, then check real bytes rather than trusting bitrate.
    bitrate = int(TARGET_VIDEO_BYTES * 8 / 125)
    for crf in (28, 36):
        process = None
        accepted = False
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=15",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                str(crf),
                "-maxrate",
                str(bitrate),
                "-bufsize",
                str(bitrate * 2),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(destination),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}),
            )
            await asyncio.wait_for(process.wait(), 120)
            if process.returncode == 0 and destination.exists():
                if 0 < destination.stat().st_size < MAX_VIDEO_BYTES:
                    accepted = True
                    return None
        except (OSError, TimeoutError):
            pass
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            # Cancellation must also discard the incomplete output.
            if not accepted:
                destination.unlink(missing_ok=True)
    return "Video omitted: encoding failed or the complete clip could not fit below 10 MB."
