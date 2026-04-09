from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol, runtime_checkable

from asyncmy.cursors import Cursor

@runtime_checkable
class Connection(Protocol):
    def cursor(self, cursor: type[Cursor] | None = None) -> Cursor: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    def close(self) -> None: ...
    async def ensure_closed(self) -> None: ...

def connect(
    user: str | None = None,
    password: str = "",
    host: str | None = None,
    database: str | None = None,
    unix_socket: str | None = None,
    port: int = 0,
    **kwargs: Any,
) -> Awaitable[Connection]: ...
