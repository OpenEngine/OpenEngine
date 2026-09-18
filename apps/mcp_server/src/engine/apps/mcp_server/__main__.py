"""Run the loopback MCP gateway behind Tailscale Funnel."""

import argparse
import os

from dotenv import load_dotenv
import uvicorn

from .server import Settings, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, help="Private file containing OE_MCP_* settings")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    try:
        settings = Settings(
            token=os.environ["OE_MCP_TOKEN"],
            repository=os.environ["OE_MCP_REPOSITORY"],
            workflow=os.environ["OE_MCP_WORKFLOW"],
            public_url=os.environ["OE_MCP_PUBLIC_URL"],
            engine_url=os.environ.get("OE_MCP_ENGINE_URL", "http://127.0.0.1:8000"),
        )
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
