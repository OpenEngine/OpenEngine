"""Public OAuth client registration and bounded, address-pinned CIMD retrieval."""

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx

MAX_DOCUMENT = 16384


def validate_client(document):
    if not isinstance(document, dict):
        raise ValueError("client metadata must be an object")
    redirects = document.get("redirect_uris")
    if not isinstance(redirects, list) or not 1 <= len(redirects) <= 10:
        raise ValueError("redirect_uris is required")
    for value in redirects:
        if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
            raise ValueError("invalid redirect URI")
        uri = urlsplit(value)
        local = uri.hostname in {"127.0.0.1", "::1", "localhost"}
        if (not uri.hostname or uri.username or uri.password or uri.fragment
                or "\\" in value or (uri.scheme != "https" and not (uri.scheme == "http" and local))):
            raise ValueError("redirect URI requires HTTPS (HTTP allowed on loopback)")
        _ = uri.port
    if document.get("token_endpoint_auth_method", "none") != "none":
        raise ValueError("only public clients are supported")
    if document.get("response_types", ["code"]) != ["code"]:
        raise ValueError("only code responses are supported")
    grants = document.get("grant_types", ["authorization_code", "refresh_token"])
    if not isinstance(grants, list) or not grants or any(g not in ("authorization_code", "refresh_token") for g in grants):
        raise ValueError("unsupported grant types")
    name = document.get("client_name", "MCP client")
    if not isinstance(name, str) or not 1 <= len(name) <= 200:
        raise ValueError("invalid client_name")
    return {"client_name": name, "redirect_uris": redirects,
            "token_endpoint_auth_method": "none", "response_types": ["code"], "grant_types": grants}


async def fetch_cimd(client_id: str):
    uri = urlsplit(client_id)
    if (uri.scheme != "https" or not uri.hostname or uri.username or uri.password
            or uri.fragment or not uri.path or uri.path == "/" or len(client_id) > 2048
            or "\\" in client_id or any(c.isspace() for c in client_id)):
        raise ValueError("invalid HTTPS client_id")
    # Limit the whole operation, including DNS and slow streaming bodies.
    async with asyncio.timeout(5):
        addresses = await asyncio.get_running_loop().getaddrinfo(
            uri.hostname, uri.port or 443, type=socket.SOCK_STREAM,
        )
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("client metadata address is not public")
        address = addresses[0][4][0]
        # Connect to the vetted address, keeping TLS certificate verification and
        # SNI for the original host. Never resolve the hostname a second time.
        target = httpx.URL(client_id).copy_with(host=address)
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", target, headers={"Host": uri.netloc, "Accept": "application/json", "Accept-Encoding": "identity"},
                                     extensions={"sni_hostname": uri.hostname}) as response:
                if response.status_code != 200:
                    raise ValueError("metadata must return 200 without redirects")
                if response.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    raise ValueError("metadata must be JSON")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("compressed metadata is not supported")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    body.extend(chunk)
                    if len(body) > MAX_DOCUMENT:
                        raise ValueError("metadata is too large")
    document = json.loads(body)
    if not isinstance(document, dict) or document.get("client_id") != client_id:
        raise ValueError("metadata client_id mismatch")
    return validate_client(document)
