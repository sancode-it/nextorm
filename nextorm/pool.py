"""Connection pool — thread-safe pool for sync database connections.

A :class:`ConnectionPool` pre-creates *min_size* connections at construction
time and allows up to *max_size* concurrent checkouts.  Connections are
returned to the pool after each use; if *max_size* is already checked out the
caller waits up to *timeout* seconds before a :exc:`PoolTimeoutError` is
raised.

Usage::

    from nextorm import Database

    db = Database(entities=[User])
    db.bind("sqlite", "app.db", pool_min=1, pool_max=5, pool_timeout=30.0)
    db.generate_mapping()

    # Both styles of usage transparently pull a connection from the pool:
    results = db.select(User).fetch_all()

Async counterpart: :class:`AsyncConnectionPool`.
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["ConnectionPool", "AsyncConnectionPool", "PoolTimeoutError"]


class PoolTimeoutError(Exception):
    """Raised when all pool connections are in use and the wait times out."""


class ConnectionPool:
    """Thread-safe synchronous connection pool.

    Parameters
    ----------
    factory:
        Zero-argument callable that creates and returns a new connection.
    min_size:
        Number of connections to pre-create at construction time (0 is valid).
    max_size:
        Maximum total number of connections.  Once reached, :meth:`acquire`
        blocks until a connection is released or *timeout* expires.
    timeout:
        Seconds to wait for a free connection before raising
        :exc:`PoolTimeoutError`.
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        min_size: int = 1,
        max_size: int = 5,
        timeout: float = 30.0,
    ) -> None:
        if min_size < 0:
            raise ValueError("min_size must be >= 0")
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if min_size > max_size:
            raise ValueError("min_size must be <= max_size")
        self._factory = factory
        self._max_size = max_size
        self._timeout = timeout
        self._lock = threading.Lock()
        self._queue: queue.Queue[Any] = queue.Queue()
        self._size = 0
        # Pre-create min_size connections eagerly
        for _ in range(min_size):
            conn = self._create()
            self._queue.put(conn)

    def _create(self) -> Any:
        """Create a new connection and increment the total size counter."""
        conn = self._factory()
        with self._lock:
            self._size += 1
        return conn

    def acquire(self) -> Any:
        """Check out a connection from the pool.

        Returns immediately when an idle connection is available.  Creates a
        new connection when the pool is below *max_size*.  Blocks up to
        *timeout* seconds when the pool is at *max_size*.

        Raises
        ------
        PoolTimeoutError
            If no connection becomes available within the timeout.
        """
        # Fast path: try to get an idle connection without blocking
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            pass
        # Slow path: try to create a new connection if below max_size
        with self._lock:
            if self._size < self._max_size:
                conn = self._factory()
                self._size += 1
                return conn
        # Pool exhausted — wait for a release
        try:
            return self._queue.get(timeout=self._timeout)
        except queue.Empty:
            raise PoolTimeoutError(
                f"Connection pool exhausted: all {self._max_size} connections are in use "
                f"and no connection was released within {self._timeout}s."
            ) from None

    def release(self, conn: Any) -> None:
        """Return *conn* to the pool for reuse."""
        self._queue.put(conn)

    def close_all(self) -> None:
        """Close all idle connections in the pool.

        Connections that are currently checked out are not affected; they
        will be closed when :meth:`release` is called and the pool detects
        that it is shutting down (currently the pool does not track checked-out
        connections, so callers must ensure all connections are released before
        calling this method if a clean shutdown is required).
        """
        while True:
            try:
                conn = self._queue.get_nowait()
                conn.close()
                with self._lock:
                    self._size -= 1
            except queue.Empty:
                break

    @property
    def pool_size(self) -> int:
        """Total number of connections created (idle + checked out)."""
        return self._size

    @property
    def idle_count(self) -> int:
        """Number of connections currently sitting idle in the pool."""
        return self._queue.qsize()


class AsyncConnectionPool:
    """Async connection pool (uses :mod:`asyncio` synchronisation primitives).

    Parameters
    ----------
    factory:
        Async callable that creates and returns a new async connection.
    min_size:
        Number of connections to pre-create during :meth:`start`.
    max_size:
        Maximum total number of connections.
    timeout:
        Seconds to wait for a free connection before raising
        :exc:`PoolTimeoutError`.
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        min_size: int = 1,
        max_size: int = 5,
        timeout: float = 30.0,
    ) -> None:
        if min_size < 0:
            raise ValueError("min_size must be >= 0")
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if min_size > max_size:
            raise ValueError("min_size must be <= max_size")
        self._factory = factory
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout
        self._idle: list[Any] = []
        self._size = 0
        self._lock: Any = None  # created lazily on first use (asyncio.Lock)
        self._semaphore: Any = None  # created lazily (asyncio.Semaphore)

    def _ensure_primitives(self) -> None:
        """Create asyncio synchronisation objects on first call (inside event loop)."""
        import asyncio  # noqa: PLC0415

        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_size)

    async def start(self) -> None:
        """Pre-create *min_size* connections.  Must be called inside an event loop."""
        self._ensure_primitives()
        for _ in range(self._min_size):
            conn = await self._factory()
            self._idle.append(conn)
            self._size += 1

    async def acquire(self) -> Any:
        """Check out an async connection from the pool.

        Raises
        ------
        PoolTimeoutError
            If no connection becomes available within the timeout.
        """
        import asyncio  # noqa: PLC0415

        self._ensure_primitives()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._timeout)
        except TimeoutError:
            raise PoolTimeoutError(
                f"Async connection pool exhausted: all {self._max_size} connections are in use "
                f"and none was released within {self._timeout}s."
            ) from None
        async with self._lock:
            if self._idle:
                return self._idle.pop()
            conn = await self._factory()
            self._size += 1
            return conn

    async def release(self, conn: Any) -> None:
        """Return *conn* to the pool for reuse."""
        self._ensure_primitives()
        async with self._lock:
            self._idle.append(conn)
        self._semaphore.release()

    async def close_all(self) -> None:
        """Close all idle connections."""
        self._ensure_primitives()
        async with self._lock:
            for conn in self._idle:
                await conn.close()
                self._size -= 1
            self._idle.clear()

    @property
    def pool_size(self) -> int:
        """Total number of connections created."""
        return self._size

    @property
    def idle_count(self) -> int:
        """Number of idle connections."""
        return len(self._idle)
