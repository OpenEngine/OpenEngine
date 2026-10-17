"""One host Unix socket forwarded to a private guest control path."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class SandboxSocket:
    host: Path
    guest: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.guest)
        if (
            not path.is_absolute()
            or not self.guest.startswith("/run/openengine/")
            or ".." in path.parts
            or any(c in self.guest + str(self.host) for c in "\0:")
        ):
            message = "sandbox sockets must use a private /run/openengine control path"
            raise ValueError(message)
