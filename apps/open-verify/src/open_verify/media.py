"""Bounded GIF summaries and legacy MP4 encoding."""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

MAX_VIDEO_BYTES = 10_000_000
TARGET_VIDEO_BYTES = 9_000_000


async def encode_gif(screenshots: list[Path], destination: Path) -> str | None:
    """A labeled-by-filename slideshow, not a recording of provider authentication."""
    if len(screenshots) < 2:
        return "GIF omitted: at least two distinct app checkpoints are required."
    if len(screenshots) > 100:
        return 'GIF omitted: too many checkpoints; no frames were silently dropped.'
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            return "GIF omitted: install open-verify[browser] or provide ffmpeg."
    process = None
    accepted = False
    try:
        with tempfile.TemporaryDirectory(prefix='ov-login-gif-') as directory:
            root = Path(directory)
            frames = []
            for index, source in enumerate(screenshots):
                # Fixed local filenames avoid interpreting source paths as concat syntax.
                shutil.copyfile(source, root / f'frame-{index}.png')
                frames.extend([f"file 'frame-{index}.png'", f"duration {3 if index == len(screenshots) - 1 else 2}"])
            frames.append(f"file 'frame-{len(screenshots) - 1}.png'")
            (root / 'frames.txt').write_text('\n'.join(frames) + '\n')
            process = await asyncio.create_subprocess_exec(
                ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'concat', '-safe', '1', '-i', 'frames.txt',
                '-vf', "fps=2,scale='min(960,iw)':-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse",
                '-t', str(2 * (len(screenshots) - 1) + 3), '-loop', '0', str(destination.resolve()),
                cwd=root, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), 60)
            if process.returncode == 0 and destination.exists() and 0 < destination.stat().st_size < MAX_VIDEO_BYTES:
                accepted = True
                return None
    except (OSError, TimeoutError):
        pass
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if not accepted:
            destination.unlink(missing_ok=True)
    return "GIF omitted: encoding failed or exceeded 10 MB."


encode_login_gif = encode_gif


async def encode_video(source: Path, destination: Path) -> str | None:
    """Return an omission reason on failure; never return a partial/oversized clip."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            from imageio_ffmpeg import get_ffmpeg_exe

            ffmpeg = get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            return "Video omitted: install open-verify[browser] or provide ffmpeg on PATH."
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
