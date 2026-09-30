import asyncio
import sys

import pytest

from open_verify.media import MAX_VIDEO_BYTES, encode_video


def test_missing_encoder_is_an_explicit_omission(tmp_path, monkeypatch):
    monkeypatch.setattr("open_verify.media.shutil.which", lambda _: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    reason = asyncio.run(encode_video(tmp_path / "input.webm", tmp_path / "out.mp4"))
    assert "install open-verify[browser]" in reason
    assert not (tmp_path / "out.mp4").exists()


@pytest.mark.parametrize(
    "sizes, accepted",
    [
        ([MAX_VIDEO_BYTES - 1], True),
        ([MAX_VIDEO_BYTES, 100], True),
        ([MAX_VIDEO_BYTES, MAX_VIDEO_BYTES + 1], False),
        ([0, 0], False),
    ],
)
def test_encoder_checks_actual_bytes_and_retries_or_omits(tmp_path, monkeypatch, sizes, accepted):
    monkeypatch.setattr("open_verify.media.shutil.which", lambda _: "ffmpeg")
    calls = []

    class Process:
        returncode = 0

        async def wait(self):
            return 0

    async def spawn(*args, **kwargs):
        with open(args[-1], "wb") as file:
            file.truncate(sizes[len(calls)])
        calls.append(args)
        return Process()

    monkeypatch.setattr("open_verify.media.asyncio.create_subprocess_exec", spawn)
    output = tmp_path / "out.mp4"
    reason = asyncio.run(encode_video(tmp_path / "input.webm", output))
    assert (reason is None) == accepted
    assert output.exists() == accepted
    assert len(calls) == len(sizes)
    assert all("libx264" in call for call in calls)


def test_cancelled_encoder_is_killed_and_partial_file_removed(tmp_path, monkeypatch):
    monkeypatch.setattr("open_verify.media.shutil.which", lambda _: "ffmpeg")

    class Process:
        returncode = None
        killed = False

        async def wait(self):
            if not self.killed:
                raise asyncio.CancelledError()
            return 1

        def kill(self):
            self.killed = True
            self.returncode = 1

    process = Process()
    output = tmp_path / "out.mp4"

    async def spawn(*args, **kwargs):
        output.write_bytes(b"partial")
        return process

    monkeypatch.setattr("open_verify.media.asyncio.create_subprocess_exec", spawn)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(encode_video(tmp_path / "input.webm", output))
    assert process.killed
    assert not output.exists()
