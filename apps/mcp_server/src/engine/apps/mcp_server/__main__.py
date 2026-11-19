"""Run the loopback MCP gateway behind Tailscale Funnel."""

import argparse

from dotenv import load_dotenv
import uvicorn

from .server import Settings, create_app, engine_login_problem


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, help="Private file containing OE_MCP_* settings")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    try:
        settings = Settings.from_env()
    except ValueError as error:
        parser.error(str(error))
    if problem := engine_login_problem(settings):
        parser.error(problem)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
