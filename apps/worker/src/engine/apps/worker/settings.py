"""Settings for the worker."""

from dataclasses import dataclass

from engine.runtime import ServiceSettings


@dataclass(frozen=True, slots=True)
class Settings(ServiceSettings):
    """Everything the worker needs, layered on the loaded Engine config."""

    task_queue: str = "engine"
