"""Static type assertions for public NextORM APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nextorm import PK, Entity

if TYPE_CHECKING:
    from typing import assert_type

    class LookupEntity(Entity):
        id: PK[int]

    assert_type(LookupEntity[1], LookupEntity)
