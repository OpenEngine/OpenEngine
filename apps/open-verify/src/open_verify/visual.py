"""Bounded visual evidence shared by engines, runners and image-capable transports."""

import base64
import hashlib
import os
import stat
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class VisualUnavailable(ValueError):
    """Visual input cannot be provided safely or is not supported."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VisualImage:
    """One validated PNG; bytes stay outside prompts, reports and exception reprs."""

    data: bytes = field(repr=False)
    width: int = field(init=False)
    height: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self):
        """Check bounded PNG framing, dimensions and checksums before transport."""
        data = self.data
        if not isinstance(data, bytes) or len(data) > MAX_IMAGE_BYTES or not data.startswith(PNG_SIGNATURE):
            raise VisualUnavailable('VISUAL_EVIDENCE_INVALID', 'Expected a PNG no larger than 4 MiB')
        offset, dimensions, image_data, ended = 8, None, bytearray(), False
        row_bytes = 0
        while offset + 12 <= len(data):
            size = struct.unpack('>I', data[offset:offset + 4])[0]
            end = offset + 12 + size
            if end > len(data):
                break
            kind, payload = data[offset + 4:offset + 8], data[offset + 8:end - 4]
            crc = struct.unpack('>I', data[end - 4:end])[0]
            if zlib.crc32(kind + payload) != crc:
                break
            if offset == 8:
                if kind != b'IHDR' or size != 13:
                    break
                dimensions = struct.unpack('>II', payload[:8])
                width, height = dimensions
                depth, color, compression, filtering, interlace = payload[8:]
                channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
                if depth != 8 or channels is None or compression or filtering or interlace:
                    raise VisualUnavailable('VISUAL_EVIDENCE_INVALID', 'Expected an 8-bit non-interlaced browser PNG')
                row_bytes = width * channels + 1
                if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= MAX_IMAGE_PIXELS):
                    raise VisualUnavailable('VISUAL_EVIDENCE_INVALID', 'PNG dimensions exceed the visual evidence limit')
            elif kind == b'IHDR':
                break
            if kind == b'IDAT':
                image_data.extend(payload)
            offset = end
            if kind == b'IEND':
                ended = size == 0 and offset == len(data)
                break
        if dimensions is None or not image_data or not ended:
            raise VisualUnavailable('VISUAL_EVIDENCE_INVALID', 'PNG evidence is incomplete or corrupt')
        try:
            decoder = zlib.decompressobj()
            pixels = decoder.decompress(image_data, row_bytes * dimensions[1] + 1)
            if (len(pixels) != row_bytes * dimensions[1] or not decoder.eof
                    or decoder.unused_data or decoder.unconsumed_tail
                    or any(pixels[i] > 4 for i in range(0, len(pixels), row_bytes))):
                raise ValueError('Invalid pixel rows')
        except (zlib.error, ValueError) as exc:
            raise VisualUnavailable('VISUAL_EVIDENCE_INVALID', 'PNG pixel data is corrupt') from exc
        object.__setattr__(self, 'width', dimensions[0])
        object.__setattr__(self, 'height', dimensions[1])
        object.__setattr__(self, 'sha256', hashlib.sha256(data).hexdigest())

    def content_block(self):
        """Use ACP image content, never a local path or base64 embedded in prose."""
        return {'type': 'image', 'mimeType': 'image/png',
                'data': base64.b64encode(self.data).decode('ascii')}

    def metadata(self):
        """Describe exactly the attached pixels without including their bytes."""
        return {'mime_type': 'image/png', 'width': self.width, 'height': self.height,
                'sha256': self.sha256, 'scope': 'viewport'}


def load_visual(artifacts: Path, observation: dict) -> VisualImage:
    """Read only bounded, digest-matched PNG evidence directly inside the run directory."""
    name = observation.get('screenshot')
    if not isinstance(name, str) or Path(name).name != name or not name.endswith('.png'):
        raise VisualUnavailable('VISUAL_EVIDENCE_INVALID', 'Visual evidence must name a PNG inside the run directory')
    try:
        before = (artifacts / name).lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Not a regular image file')
        fd = os.open(artifacts / name, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMAGE_BYTES
                    or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError('Not a bounded regular image file')
            image = VisualImage(stream.read(MAX_IMAGE_BYTES + 1))
        if observation.get('image') != image.metadata():
            raise ValueError('Image receipt does not match its pixels')
        return image
    except (OSError, ValueError) as exc:
        raise VisualUnavailable('VISUAL_EVIDENCE_INVALID', 'Visual evidence is missing, invalid or changed since capture') from exc
