"""Chromium page/frame interception with recursive child-target handling."""

import asyncio
import json


class TargetDetached(RuntimeError):
    pass


class ChildSession:
    """Public CDP Target messaging for an auto-attached, non-flat session.

    Playwright's CDPSession.send cannot address an arbitrary child session ID.
    Target.sendMessageToTarget keeps this transport on the public protocol and
    works recursively for frames within frames, without Playwright internals.
    """

    def __init__(self, parent, session_id):
        self.parent = parent
        self.session_id = session_id
        self.handlers = {}
        self.pending = {}
        self.sequence = 0
        self.closed = False

    def on(self, event, callback):
        self.handlers[event] = callback

    async def send(self, method, params=None):
        if self.closed:
            raise TargetDetached("Browser target detached")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(10):
                await self.parent.send(
                    "Target.sendMessageToTarget",
                    {
                        "sessionId": self.session_id,
                        "message": json.dumps(
                            {"id": request_id, "method": method, "params": params or {}}
                        ),
                    },
                )
                return await future
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def receive(self, message):
        if "id" in message:
            future = self.pending.get(message["id"])
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(RuntimeError(str(message["error"])))
                else:
                    future.set_result(message.get("result", {}))
        else:
            handler = self.handlers.get(message.get("method"))
            if handler:
                handler(message.get("params", {}))

    def close(self):
        self.closed = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(TargetDetached("Browser target detached"))


class BrowserGuard:
    def __init__(self, check_url, events, context):
        self.check_url = check_url
        self.events = events
        self.context = context
        self.children = {}
        self.tasks = set()
        self.closing = False
        self.protected_targets = []

    def schedule(self, operation):
        if self.closing:
            operation.close()
            return

        async def run():
            try:
                await operation
            except TargetDetached:
                pass
            except Exception as exc:
                if not self.closing:
                    self.events.append({"interception_error": str(exc)})
                    # A failed child setup must never resume an unprotected target.
                    self.closing = True
                    try:
                        await self.context.close()
                    except Exception as close_error:
                        self.events.append({"interception_cleanup_error": str(close_error)})

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def install(self, session, target_type="page", *, resume=False):
        def attached(event):
            if self.closing:
                return
            child = ChildSession(session, event["sessionId"])
            self.children[child.session_id] = child
            self.schedule(self.install(child, event["targetInfo"]["type"], resume=True))

        def received(event):
            child = self.children.get(event["sessionId"])
            if child is not None:
                child.receive(json.loads(event["message"]))

        def detached(event):
            self.detach(event["sessionId"])

        session.on("Target.attachedToTarget", attached)
        session.on("Target.receivedMessageFromTarget", received)
        session.on("Target.detachedFromTarget", detached)
        # Dedicated workers do not expose Fetch. Their HTTP requests use
        # the owning frame's interceptor; the browser proxy covers worker HTTP
        # and WebSocket traffic even before CDP setup completes.
        if target_type not in {"worker", "shared_worker", "service_worker"}:
            session.on(
                "Fetch.requestPaused",
                lambda event: self.schedule(self.check_request(session, event)),
            )
            await session.send(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
            )
        # setAutoAttach covers direct children only, so every child installs
        # this same gate before Runtime.runIfWaitingForDebugger releases it.
        await session.send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": False,
            },
        )
        self.protected_targets.append(target_type)
        if resume:
            await session.send("Runtime.runIfWaitingForDebugger")

    def detach(self, session_id):
        child = self.children.pop(session_id, None)
        if child is not None:
            for descendant in tuple(self.children.values()):
                if descendant.parent is child:
                    self.detach(descendant.session_id)
            child.close()

    async def check_request(self, session, event):
        request_id = event["requestId"]
        url = event["request"]["url"]
        try:
            self.check_url(url)
        except ValueError:
            self.events.append({"blocked_url": url})
            await session.send(
                "Fetch.failRequest",
                {
                    "requestId": request_id,
                    "errorReason": "BlockedByClient",
                },
            )
        else:
            await session.send("Fetch.continueRequest", {"requestId": request_id})

    async def close(self):
        self.closing = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for child in self.children.values():
            child.close()
        self.children.clear()
        self.tasks.clear()
