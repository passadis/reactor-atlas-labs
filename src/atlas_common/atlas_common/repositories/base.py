"""Shared plumbing for the typed repositories.

Every repository call has an explicit timeout and raises exactly one exception type on
infrastructure failure: `RepositoryUnavailableError`. Not-found is not an error - it is `None` or an
empty list. That split is what lets the agent tools in Phase 03 turn any backend failure into the
user-visible degraded message CLAUDE.md section 6 requires ("I could not reach the ticketing
system") without ever letting a stack trace reach the model or the screen.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, Self

from azure.core.credentials_async import AsyncTokenCredential
from azure.cosmos.aio import ContainerProxy, CosmosClient, DatabaseProxy
from pydantic import BaseModel

from ..auth import get_async_credential
from ..config import Settings

DEFAULT_TIMEOUT_S = 10.0


class RepositoryUnavailableError(RuntimeError):
    """A backend could not be reached, timed out, or refused the call.

    `backend` names what failed in words a presenter can read out loud.
    """

    def __init__(self, backend: str, reason: str) -> None:
        super().__init__(f"{backend} unavailable: {reason}")
        self.backend = backend
        self.reason = reason


async def with_timeout[T](
    awaitable: Awaitable[T], backend: str, timeout_s: float = DEFAULT_TIMEOUT_S
) -> T:
    """Await with a hard timeout, mapping every failure to `RepositoryUnavailableError`."""
    try:
        async with asyncio.timeout(timeout_s):
            return await awaitable
    except TimeoutError:
        raise RepositoryUnavailableError(backend, f"no response within {timeout_s:.0f}s") from None
    except RepositoryUnavailableError:
        raise
    except Exception as exc:
        raise RepositoryUnavailableError(backend, type(exc).__name__) from exc


def to_model[M: BaseModel](model: type[M], document: dict[str, Any]) -> M:
    """Validate a stored document into a model, dropping storage-only fields.

    Cosmos adds `id`, `_rid`, `_ts` and friends, and the seeder adds `source`. The domain models
    forbid unknown fields, so those are filtered here rather than loosening the models.
    """
    return model.model_validate({k: v for k, v in document.items() if k in model.model_fields})


class CosmosStore:
    """One Cosmos client per process, shared by every repository.

    Use as an async context manager so the client and credential are always closed.
    """

    def __init__(self, settings: Settings, credential: AsyncTokenCredential | None = None) -> None:
        self._settings = settings
        self._owns_credential = credential is None
        self._credential = credential or get_async_credential()
        self._client = CosmosClient(settings.cosmos_endpoint, credential=self._credential)
        self._database: DatabaseProxy = self._client.get_database_client(settings.cosmos_database)

    def container(self, name: str) -> ContainerProxy:
        return self._database.get_container_client(name)

    async def query(
        self,
        container: str,
        sql: str,
        parameters: list[dict[str, Any]] | None = None,
        *,
        partition_key: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> list[dict[str, Any]]:
        """Run a parameterised query and collect the results, under a timeout."""
        proxy = self.container(container)

        async def collect() -> list[dict[str, Any]]:
            kwargs: dict[str, Any] = {"parameters": parameters or []}
            if partition_key is not None:
                kwargs["partition_key"] = partition_key
            return [item async for item in proxy.query_items(sql, **kwargs)]

        return await with_timeout(collect(), f"Cosmos {container}", timeout_s)

    async def upsert(
        self, container: str, document: dict[str, Any], *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        """Create or replace one document, under a timeout."""
        await with_timeout(
            self.container(container).upsert_item(document), f"Cosmos {container}", timeout_s
        )

    async def delete(
        self,
        container: str,
        item_id: str,
        partition_key: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> bool:
        """Delete one document. Returns False if it did not exist; that is not an error."""
        from azure.core.exceptions import ResourceNotFoundError

        async def run() -> bool:
            try:
                await self.container(container).delete_item(item_id, partition_key=partition_key)
            except ResourceNotFoundError:
                return False
            return True

        return await with_timeout(run(), f"Cosmos {container}", timeout_s)

    async def close(self) -> None:
        await self._client.close()
        if self._owns_credential:
            await self._credential.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


@asynccontextmanager
async def cosmos_store(settings: Settings | None = None) -> AsyncIterator[CosmosStore]:
    """Convenience: `async with cosmos_store() as store: ...`"""
    store = CosmosStore(settings or Settings.from_env())
    try:
        yield store
    finally:
        await store.close()
