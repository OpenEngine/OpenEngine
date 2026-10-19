"""The boot screen `oe up` prints while OpenEngine starts."""

from __future__ import annotations

# figlet's "small" font, the one NATS uses for its JetStream banner.
BANNER = r"""
  ___  ___ ___ _  _ ___ _  _  ___ ___ _  _ ___
 / _ \| _ \ __| \| | __| \| |/ __|_ _| \| | __|
| (_) |  _/ _|| .` | _|| .` | (_ || || .` | _|
 \___/|_| |___|_|\_|___|_|\_|\___|___|_|\_|___|
"""


def banner() -> str:
    return BANNER.strip("\n")
