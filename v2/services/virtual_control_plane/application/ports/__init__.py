"""Only these interfaces are required by application use cases."""
from contextlib import AbstractAsyncContextManager
from typing import Protocol


class Transaction(Protocol):
    async def get(self, collection: str, key: str) -> dict | None: ...
    async def put(self, collection: str, key: str, value: dict) -> None: ...
    async def all(self, collection: str) -> list[dict]: ...


class Storage(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Transaction]: ...


class Compute(Protocol):
    capabilities: dict
    def validate(self, specification: dict) -> None: ...
    async def provision(self, ownership: dict, specification: dict) -> None:
        """Reconcile this durable ownership key, including an interrupted creation."""
        ...
    async def inspect(self, ownership: dict) -> bool: ...
    async def destroy(self, ownership: dict, retention: dict) -> None: ...


class Authorizer(Protocol):
    async def authorize(self, actor: str | None, subject: str, owner: str | None) -> None: ...


class Publisher(Protocol):
    async def publish(self, event: dict) -> None:
        """Return only after durable broker acknowledgement."""
        ...


class ExecutionContext(Protocol):
    async def prepare(self, workspace_record: dict, runtime: dict, payload: dict) -> dict:
        """Validate runner support and assemble a runtime context without dispatching.

        Called inside acceptance's transaction: must be bounded and have no
        external side effects. Runtime readiness is supplied by the adapter,
        never inferred from a VM being powered on.
        """
        ...


class RuntimeCommandEncoder(Protocol):
    def encode_steer(self, message: dict) -> bytes: ...

    def encode_cancel(self, message: dict) -> bytes: ...

    def encode_execute(self, message: dict) -> bytes:
        """Validate the internal contract and wire size, then return exact UTF-8 bytes."""
        ...
