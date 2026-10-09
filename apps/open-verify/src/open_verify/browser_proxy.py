"""An origin-checking HTTP/CONNECT proxy for Chromium, including worker traffic."""

import asyncio
from contextlib import suppress
from urllib.parse import urlsplit


class BrowserProxy:
    def __init__(self, check_url, events):
        self.check_url = check_url
        self.events = events
        self.server = None
        self.tasks = set()
        self.closing = False

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def allowed(self, url):
        try:
            self.check_url(url)
        except ValueError:
            return False
        return True

    async def handle(self, reader, writer):
        if self.closing:
            writer.transport.abort()
            return
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        pumps = []
        try:
            async with asyncio.timeout(15):
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header[:-4].split(b"\r\n")
                method, target, version = lines[0].decode("ascii").split(" ")
                tunnel = method == "CONNECT"
                url = "https://" + target + "/" if tunnel else target
                parsed = urlsplit(url)
                if tunnel:
                    if parsed.netloc != target:
                        raise ValueError("CONNECT requires an authority, without a path")
                elif parsed.scheme not in {"http", "ws"}:
                    raise ValueError("Encrypted requests must use CONNECT")
                if parsed.scheme == "ws":
                    url = parsed._replace(scheme="http").geturl()
                port = parsed.port or (443 if tunnel else 80)
                plain_url = f"http://{parsed.netloc}/" if tunnel else url
                if tunnel and parsed.port is None:
                    plain_url = f"http://{parsed.netloc}:443/"
                if not self.allowed(url) and not (tunnel and self.allowed(plain_url)):
                    self.events.append({"blocked_url": url})
                    writer.write(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    await writer.drain()
                    return
                if tunnel:
                    # Chromium also uses CONNECT for unencrypted WebSockets.
                    # Read the protocol before opening any upstream connection,
                    # so allowing HTTP never implicitly permits HTTPS (or vice versa).
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    prefix = await reader.readexactly(1)
                    if prefix == b"\x16":  # TLS ClientHello; leave TLS end-to-end.
                        actual_url = url
                    else:
                        prefix += await reader.readuntil(b"\r\n\r\n")
                        if (
                            not prefix.startswith(b"GET ")
                            or b"\r\nupgrade: websocket\r\n" not in prefix.lower()
                        ):
                            raise ValueError("Plain CONNECT is reserved for WebSockets")
                        actual_url = plain_url
                    if not self.allowed(actual_url):
                        self.events.append({"blocked_url": actual_url})
                        return
                    remote, upstream = await asyncio.open_connection(
                        parsed.hostname, port, happy_eyeballs_delay=0.25
                    )
                    upstream.write(prefix)
                    await upstream.drain()
                else:
                    remote, upstream = await asyncio.open_connection(
                        parsed.hostname, port, happy_eyeballs_delay=0.25
                    )
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    headers = [
                        line
                        for line in lines[1:]
                        if not line.lower().startswith(
                            (b"proxy-authorization:", b"proxy-connection:", b"connection:")
                        )
                    ]
                    upgrade = any(
                        line.lower().startswith(b"upgrade: websocket") for line in headers
                    )
                    headers.append(b"Connection: Upgrade" if upgrade else b"Connection: close")
                    upstream.write(
                        f"{method} {path} {version}\r\n".encode("ascii")
                        + b"\r\n".join(headers)
                        + b"\r\n\r\n"
                    )
                    await upstream.drain()

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            pumps = [
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(remote, writer)),
            ]
            # An HTTP client can half-close after sending its body and still
            # expect a response. Only upstream EOF completes the response.
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            if pumps[0] in done and not pumps[0].cancelled() and pumps[0].exception() is None:
                if upstream.can_write_eof():
                    upstream.write_eof()
                await pumps[1]
        except (
            OSError,
            ValueError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            # A cancelled navigation or a peer closing a connection is normal.
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (upstream, writer):
                if stream is not None:
                    if self.closing or task.cancelling():
                        stream.transport.abort()
                    else:
                        stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()
            self.tasks.discard(task)

    async def close(self):
        self.closing = True
        if self.server is not None:
            self.server.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()
            self.server = None
