"""Control server entrypoint.

Ticket 1 stops at composition: it builds the capability graph and reports it.
The HTTP surface that accepts run requests lands with the control-server ticket.
"""

from engine.apps.control_server.composition import Settings, build_capabilities


def main() -> None:
    capabilities = build_capabilities(Settings())
    print("engine control server -- capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    print("no ingress yet; see Ticket 1 acceptance criteria.")


if __name__ == "__main__":
    main()
