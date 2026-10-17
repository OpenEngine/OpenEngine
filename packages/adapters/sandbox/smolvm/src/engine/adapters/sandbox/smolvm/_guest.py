"""Standalone transfer helper, copied into the guest. Standard library only."""

import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

WORKSPACE = Path("/workspace")
MEMORY_HOME_ROOT = Path("/dev/shm")
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10_000

# Guest defaults, never host values. Explicit exec env and spec secrets extend
# this list by name. Keep the helper standalone: it is copied into the image.
GUEST_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
})


def _is_tmpfs(path: Path) -> bool:
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] == str(path) and line.split(" - ", 1)[1].split()[0] == "tmpfs":
            return True
    return False


def _home_path(home: Path, relative: str) -> Path:
    path = Path(relative)
    target = home / path
    _check_destination(target)
    if path.is_absolute() or not relative or not target.resolve().is_relative_to(home):
        message = "private home path leaves its home"
        raise ValueError(message)
    return target


def create_home(payload: dict) -> str:
    if not _is_tmpfs(MEMORY_HOME_ROOT):
        message = "model credentials require a tmpfs guest home"
        raise RuntimeError(message)
    home = Path(tempfile.mkdtemp(prefix="oe-home-", dir=MEMORY_HOME_ROOT)).resolve()
    previous = os.umask(0o077)
    try:
        for relative, value in payload["files"].items():
            path = _home_path(home, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(value)
        for relative in (".tmp", ".config", ".cache", ".local/share", *payload["paths"].values()):
            _home_path(home, relative).mkdir(parents=True, exist_ok=True)
        (home / ".oe-home.json").write_text(json.dumps({
            "paths": payload["paths"], "env_files": payload["env_files"],
        }))
        return str(home)
    except BaseException:
        shutil.rmtree(home)
        raise
    finally:
        os.umask(previous)


def home_environment(value: str) -> dict[str, str]:
    home = Path(value).resolve()
    if home == MEMORY_HOME_ROOT.resolve() or not home.is_relative_to(MEMORY_HOME_ROOT.resolve()):
        message = "private home must stay inside guest tmpfs"
        raise ValueError(message)
    config = json.loads((home / ".oe-home.json").read_text())
    environment = {
        "HOME": str(home), "TMPDIR": str(home / ".tmp"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    environment.update({key: str(_home_path(home, path)) for key, path in config["paths"].items()})
    environment.update({key: _home_path(home, path).read_text() for key, path in config["env_files"].items()})
    return environment


def remove_home(value: str) -> None:
    home = Path(value)
    if home.parent.resolve() != MEMORY_HOME_ROOT.resolve() or not home.name.startswith("oe-home-"):
        message = "private home cleanup must stay inside guest tmpfs"
        raise ValueError(message)
    if home.is_symlink():
        home.unlink()
    elif home.exists():
        home.chmod(0o700)
        for directory, _, _ in os.walk(home, followlinks=False):
            Path(directory).chmod(0o700)
        shutil.rmtree(home)


def require_patched_python() -> None:
    version = sys.version_info[:3]
    floors = {(3, 11): (3, 11, 13), (3, 12): (3, 12, 11), (3, 13): (3, 13, 4)}
    if version < floors.get(version[:2], (3, 14, 0)):
        message = "sandbox transfers require patched Python: 3.11.13+, 3.12.11+, 3.13.4+ or 3.14+"
        raise RuntimeError(message)


def workspace_path(relative: str, *, destination: bool = False) -> Path:
    path = Path(relative)
    original = WORKSPACE / path
    if destination:
        _check_destination(original)
    resolved = original.resolve()
    if path.is_absolute() or not resolved.is_relative_to(WORKSPACE.resolve()):
        message = "sandbox path must stay within its workspace"
        raise ValueError(message)
    return resolved


def pack(source: Path, archive: Path) -> None:
    # One known root lets the receiver copy either a file or a whole directory.
    def metadata(member: tarfile.TarInfo) -> tarfile.TarInfo:
        if member.name == "payload":
            member.pax_headers = {**member.pax_headers, "openengine.basename": source.name}
        return member

    with tarfile.open(archive, "w") as output:
        output.add(source, arcname="payload", recursive=True, filter=metadata)


def _check_destination(destination: Path) -> None:
    # Reject existing links at every destination component, including ancestors.
    for part in (destination, *destination.parents):
        if part.is_symlink():
            message = "copy destination must not contain a symlink"
            raise ValueError(message)


def _copy(source: Path, destination: Path, *, export: bool = False) -> None:
    _check_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(source.readlink())
    elif source.is_dir():
        destination.mkdir(exist_ok=True)
        for child in source.iterdir():
            _copy(child, destination / child.name, export=export)
        shutil.copystat(source, destination)
    elif destination.is_dir():
        _copy(source, destination / source.name, export=export)
    elif export:
        # Do not overwrite a file even if it appeared after preflight.
        with source.open("rb") as incoming, destination.open("xb") as output:
            shutil.copyfileobj(incoming, output)
        shutil.copystat(source, destination)
    else:
        shutil.copy2(source, destination)


def _check_links(source: Path, destination: Path, source_root: Path, destination_root: Path) -> None:
    if source.is_symlink():
        target = source.readlink()
        # Extraction validates against its temporary root, one level above
        # payload. Check both the copied tree and its final host location.
        if (target.is_absolute()
                or not source.resolve().is_relative_to(source_root)
                or not (destination.parent / target).resolve().is_relative_to(destination_root)):
            message = "copied symlink must stay within its copied tree"
            raise ValueError(message)
    elif source.is_dir():
        for child in source.iterdir():
            _check_links(child, destination / child.name, source_root, destination_root)


def _check_export_destination(destination: Path) -> None:
    _check_destination(destination)
    if any(part.casefold() == ".git" for part in destination.absolute().parts):
        message = "copy-out must not target Git metadata"
        raise ValueError(message)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        message = "copy-out requires a fresh path or an empty staging directory"
        raise ValueError(message)


def unpack(archive: Path, destination: Path, export: bool = False) -> None:
    require_patched_python()
    if export:
        _check_export_destination(destination)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        message = "transfer archive exceeds byte budget"
        raise ValueError(message)
    count = total = 0

    def bounded_data(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        nonlocal count, total
        if export and any(part.casefold() == ".git" for part in Path(member.name).parts):
            message = "copy-out must not contain Git metadata"
            raise ValueError(message)
        if member.size < 0:
            message = "invalid transfer member size"
            raise ValueError(message)
        count += 1
        total += member.size
        if count > MAX_MEMBERS or total > MAX_EXTRACTED_BYTES:
            message = "transfer exceeds extracted byte or member budget"
            raise ValueError(message)
        # Re-copying hard links as independent files would amplify disk usage.
        if member.islnk():
            message = "transfer hard links are not supported"
            raise ValueError(message)
        return tarfile.data_filter(member, path)

    with tempfile.TemporaryDirectory(prefix="oe-transfer-") as temporary:
        root = Path(temporary).resolve()
        try:
            with tarfile.open(archive, "r:") as incoming:
                # data filtering refuses escaping links, devices and traversal.
                incoming.extractall(root, filter=bounded_data)
                basename = incoming.getmember("payload").pax_headers.get("openengine.basename", "payload")
            if not basename or basename in {".", ".."} or any(char in basename for char in "/\\\0"):
                message = "invalid transfer basename"
                raise ValueError(message)
            if export and basename.casefold() == ".git":
                message = "copy-out must not contain Git metadata"
                raise ValueError(message)
            source = root / "payload"
            _check_destination(destination)
            if source.is_symlink():
                message = "copied symlink must stay within its copied tree"
                raise ValueError(message)
            if not source.is_dir() and destination.is_dir():
                destination = destination / basename
            _check_links(source, destination, source, destination.resolve())
            _copy(source, destination, export=export)
        finally:
            # Read-only directory modes must not prevent temporary-file cleanup,
            # including after extraction or destination validation fails.
            for directory, _, _ in os.walk(root):
                Path(directory).chmod(0o700)



def session_directory(home: str, provider: str) -> Path:
    # These are the only durable guest directories; auth/config/MCP files stay
    # in tmpfs. Validate the home just as exec does before resolving descendants.
    home_environment(home)
    relative = {"codex": ".codex/sessions", "claude": ".claude/projects"}[provider]
    result = _home_path(Path(home), relative)
    for path in (result, *result.parents):
        if path == Path(home):
            break
        if path.is_symlink():
            message = "agent session directory must not be a symlink"
            raise ValueError(message)
    return result


def session_transfer(operation: str, home: str, provider: str, archive: str) -> None:
    directory = session_directory(home, provider)
    with tempfile.TemporaryDirectory(prefix="oe-session-") as temporary:
        root = Path(temporary).resolve()
        if operation == "session-pack":
            payload = root / "payload"
            payload.mkdir()
            if directory.exists():
                # Refuse all links, including links to credentials within HOME.
                if any(path.is_symlink() for path in directory.rglob("*")):
                    message = "agent sessions cannot contain symlinks"
                    raise ValueError(message)
                shutil.copytree(directory, payload / directory.name)
            pack(payload, Path(archive))
        else:
            payload = root / "payload"
            unpack(Path(archive), payload, True)
            for path in payload.rglob("*"):
                if path.is_symlink() or path.relative_to(payload).parts[0] != directory.name:
                    message = "invalid agent session checkpoint"
                    raise ValueError(message)
            if (payload / directory.name).exists():
                directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copytree(payload / directory.name, directory, dirs_exist_ok=True)



def credential_files(home: str, names: str) -> None:
    # Fixed host-selected names only; never traverse links or emit file data in
    # failures. Bounds apply on both sides of this private control channel.
    values = {}
    for name in json.loads(names):
        path = _home_path(Path(home), name)
        if not path.exists():
            values[name] = None
            continue
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
            message = "invalid guest credential file"
            raise ValueError(message)
        with path.open("rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            message = "guest credential exceeds byte budget"
            raise ValueError(message)
        try:
            values[name] = data.decode("utf-8")
        except UnicodeError:
            message = "guest credential is not UTF-8"
            raise ValueError(message) from None
    print(json.dumps(values))


def _dispatch(operation: str, arguments: list[str]) -> None:
    if operation == "init":
        WORKSPACE.mkdir(parents=True, exist_ok=True)
    elif operation == "home":
        payload = json.loads(sys.stdin.buffer.read(256 * 1024 + 1))
        print(json.dumps(create_home(payload)))
    elif operation == "credentials":
        credential_files(*arguments)
    elif operation == "home-remove":
        remove_home(arguments[0])
    elif operation in {"session-pack", "session-unpack"}:
        session_transfer(operation, *arguments)
    elif operation == "cwd":
        directory = workspace_path(arguments[0])
        if not directory.is_dir():
            message = "sandbox working directory does not exist"
            raise FileNotFoundError(message)
        print(json.dumps(str(directory)))
    elif operation == "exec":
        cwd, keys, *command = arguments
        allowed = GUEST_ENV_ALLOWLIST | set(json.loads(keys))
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        if command[0] == "--home":
            environment.update(home_environment(command[1]))
            os.umask(0o077)
            command = command[2:]
        os.chdir(workspace_path(cwd))
        os.execvpe(command[0], command, environment)
    elif operation == "check":
        cwd, executable = arguments
        directory = workspace_path(cwd)
        if not directory.is_dir():
            message = "sandbox working directory does not exist"
            raise FileNotFoundError(message)
        os.chdir(directory)
        resolved = shutil.which(executable)
        if resolved is None:
            message = "sandbox executable does not exist"
            raise FileNotFoundError(message)
        print(json.dumps({"cwd": str(directory), "executable": resolved}))
    elif operation == "pack":
        source, archive = arguments
        pack(workspace_path(source), Path(archive))
    elif operation == "unpack":
        archive, destination = arguments
        unpack(Path(archive), workspace_path(destination, destination=True))
    elif operation == "remove":
        Path(arguments[0]).unlink(missing_ok=True)
    elif operation == "stream":
        with Path(arguments[0]).open("rb") as archive:
            shutil.copyfileobj(archive, sys.stdout.buffer)
    else:
        message = "unknown transfer operation"
        raise ValueError(message)


def main() -> None:
    operation, *arguments = sys.argv[1:]
    try:
        _dispatch(operation, arguments)
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}))
        sys.exit(2)


if __name__ == "__main__":
    main()
