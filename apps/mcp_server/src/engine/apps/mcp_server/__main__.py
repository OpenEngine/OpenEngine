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
        # Check presence before parsing: even empty OIDC variables indicate configuration.
        if "OE_MCP_OIDC_ISSUER" not in os.environ:
            for name in ("OE_MCP_OIDC_AUDIENCE", "OE_MCP_ALLOWED_EMAILS", "OE_MCP_OIDC_REQUIRED_SCOPES"):
                if name in os.environ:
                    raise ValueError(f"{name} requires OE_MCP_OIDC_ISSUER; unset it for static-token mode")
        settings = Settings(
            token=os.environ.get("OE_MCP_TOKEN", ""),
            repository=os.environ["OE_MCP_REPOSITORY"],
            workflow=os.environ["OE_MCP_WORKFLOW"],
            public_url=os.environ["OE_MCP_PUBLIC_URL"],
            oidc_issuer=os.environ.get("OE_MCP_OIDC_ISSUER"),
            oidc_audience=os.environ.get("OE_MCP_OIDC_AUDIENCE"),
            allowed_emails=tuple(email.strip() for email in
                                 os.environ.get("OE_MCP_ALLOWED_EMAILS", "").split(",") if email.strip()),
            oidc_required_scopes=tuple(os.environ.get("OE_MCP_OIDC_REQUIRED_SCOPES", "").split()),
            engine_url=os.environ.get("OE_MCP_ENGINE_URL", "http://127.0.0.1:8000"),
        )
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
