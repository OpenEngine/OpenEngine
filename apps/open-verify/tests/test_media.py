import asyncio
import sys

import pytest

from open_verify.media import MAX_VIDEO_BYTES, encode_video


@pytest.mark.parametrize('count', [2, 10])
def test_login_gif_has_pauses_and_loops(tmp_path, count):
    import struct
    import zlib

    from open_verify.media import encode_login_gif

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    sources = []
    for index in range(count):
        color = (b'\xff\x00\x00', b'\x00\x00\xff')[index % 2]
        image = tmp_path / f'{index}.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 20, 20, 8, 2, 0, 0, 0))
                          + chunk(b'IDAT', zlib.compress((b'\x00' + color * 20) * 20)) + chunk(b'IEND', b''))
        sources.append(image)
    output = tmp_path / 'login-journey-summary.gif'
    assert asyncio.run(encode_login_gif(sources, output)) is None
    data = output.read_bytes()
    assert data.startswith(b'GIF89a') and b'NETSCAPE2.0' in data
    assert len(data) < MAX_VIDEO_BYTES
    # Parse extension blocks to check encoded playback duration, not just argv.
    position = 13 + (3 * 2 ** ((data[10] & 7) + 1) if data[10] & 128 else 0)
    duration = 0
    while data[position] != 0x3b:
        tag = data[position]
        if tag == 0x21:
            if data[position + 1] == 0xf9:
                duration += int.from_bytes(data[position + 4:position + 6], 'little') * 10
            position += 2
        elif tag == 0x2c:
            packed = data[position + 9]
            position += 10 + (3 * 2 ** ((packed & 7) + 1) if packed & 128 else 0) + 1
        else:
            pytest.fail('Invalid GIF block')
        while data[position]:
            position += data[position] + 1
        position += 1
    expected = 2000 * (count - 1) + 3000
    assert expected - 100 <= duration <= expected + 100
    assert 'two distinct' in asyncio.run(encode_login_gif(sources[:1], tmp_path / 'missing.gif'))


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
