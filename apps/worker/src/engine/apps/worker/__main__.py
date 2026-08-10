"""Worker entrypoint.

Ticket 1 stops at composition: it builds the dispatcher and reports it. Polling
a Temporal task queue lands with the workflow ticket.
"""

from engine.apps.worker.composition import Settings, build_capabilities, build_dispatcher


def main() -> None:
    settings = Settings()
    capabilities = build_capabilities(settings)
    build_dispatcher(settings)
    print(f"engine worker -- task queue {settings.task_queue!r}, capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    print("no task-queue polling yet; see Ticket 1 acceptance criteria.")


if __name__ == "__main__":
    main()
