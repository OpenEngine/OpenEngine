"""Read-only host diagnostics; checking support never boots a machine."""

from dataclasses import dataclass
import platform
import re
import shutil
import subprocess

MINIMUM_VERSION = (1, 25, 2)


@dataclass(frozen=True)
class SmolvmSupport:
    available: bool
    reason: str
    executable: str | None = None


def _kvm_problem() -> str | None:
    try:
        import fcntl

        with open("/dev/kvm", "rb+", buffering=0) as device:
            if fcntl.ioctl(device, 0xAE00) != 12:  # KVM_GET_API_VERSION
                return "/dev/kvm has an unsupported KVM API version"
    except OSError as error:
        return f"KVM is unavailable: {error}; enable virtualization and grant access to /dev/kvm"
    return None


def detect_support(executable: str = "smolvm", *, path: str | None = None) -> SmolvmSupport:
    system, architecture = platform.system(), platform.machine().lower()
    if system == "Darwin":
        if architecture not in {"arm64", "aarch64"}:
            return SmolvmSupport(False, "SmolVM requires Apple Silicon on macOS; Intel Macs are unsupported")
        release = platform.mac_ver()[0]
        if not release or int(release.split(".")[0]) < 11:
            return SmolvmSupport(False, "SmolVM requires macOS 11 or newer")
    elif system == "Linux":
        if architecture not in {"x86_64", "amd64", "aarch64", "arm64"}:
            return SmolvmSupport(False, f"unsupported Linux architecture: {architecture}")
        if problem := _kvm_problem():
            return SmolvmSupport(False, problem)
    else:
        return SmolvmSupport(False, f"OE's SmolVM backend supports macOS Apple Silicon and Linux with KVM, not {system}")
    binary = shutil.which(executable) if path is None else shutil.which(executable, path=path)
    if binary is None:
        return SmolvmSupport(False, "smolvm is not on PATH; install SmolVM 1.25.2 or newer")
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return SmolvmSupport(False, f"cannot execute smolvm: {error}", binary)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if result.returncode or not match:
        return SmolvmSupport(False, "smolvm --version failed or returned an unrecognized version", binary)
    if tuple(map(int, match.groups())) < MINIMUM_VERSION:
        return SmolvmSupport(False, "SmolVM 1.25.2 or newer is required for streamed stdio and secret references", binary)
    return SmolvmSupport(True, f"{result.stdout.strip()} available on {system}/{architecture}", binary)
