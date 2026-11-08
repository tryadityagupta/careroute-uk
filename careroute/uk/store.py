"""
uk/store.py — shared journey state that is NOT the conversation checkpoint.

    namespace       holds                                   lifetime
    -------------   -------------------------------------   ----------------
    thread          who owns a thread, whose care it is     30 days
    patient_threads referral threads per patient (inbox)    30 days
    consent         one-time consent links and grants       24 h
    episode         Path A context a later referral reuses  90 days
    audit           actions taken on someone's behalf       kept (demo)
    booking         booking ledger per patient              kept (demo)
    outbox          SMS sent (demo only, no health details) 7 days
    slot            demo provider systems: who holds a slot 30 days
    idem            idempotency results                     7 days
    fault           demo fault switches                     1 h

Same small interface in memory (one replica, tests) and Redis (replicas).
Memory mode copies on read and honours TTLs, like Redis, so "mutated but
forgot to save" bugs fail offline too.

Production note: consent grants, the audit log and the booking ledger are
records of truth and belong in Postgres tables with retention rules; Redis
keeps the demo to one extra moving part. The interface does not change.
"""

from __future__ import annotations

import copy
import json
import time
from abc import ABC, abstractmethod

DAY = 24 * 3600


class JourneyStore(ABC):
    @abstractmethod
    async def get(self, ns: str, key: str): ...

    @abstractmethod
    async def put(self, ns: str, key: str, value, ttl_s: int | None = None) -> None: ...

    @abstractmethod
    async def put_if_absent(self, ns: str, key: str, value, ttl_s: int | None = None) -> bool:
        """Atomic SET NX: True if stored, False if the key already existed."""

    @abstractmethod
    async def delete(self, ns: str, key: str) -> None: ...

    @abstractmethod
    async def append(self, ns: str, key: str, value, max_len: int = 500,
                     ttl_s: int | None = None) -> None: ...

    @abstractmethod
    async def items(self, ns: str, key: str) -> list: ...


class MemoryJourneyStore(JourneyStore):
    def __init__(self):
        self._kv: dict[tuple[str, str], tuple[str, float | None]] = {}
        self._lists: dict[tuple[str, str], tuple[list[str], float | None]] = {}

    @staticmethod
    def _expires(ttl_s):
        return time.monotonic() + ttl_s if ttl_s else None

    @staticmethod
    def _live(expires):
        return expires is None or expires > time.monotonic()

    async def get(self, ns, key):
        item = self._kv.get((ns, key))
        if item is None or not self._live(item[1]):
            self._kv.pop((ns, key), None)
            return None
        return json.loads(item[0])

    async def put(self, ns, key, value, ttl_s=None):
        self._kv[(ns, key)] = (json.dumps(value), self._expires(ttl_s))

    async def put_if_absent(self, ns, key, value, ttl_s=None):
        if await self.get(ns, key) is not None:
            return False
        await self.put(ns, key, value, ttl_s)
        return True

    async def delete(self, ns, key):
        self._kv.pop((ns, key), None)

    async def append(self, ns, key, value, max_len=500, ttl_s=None):
        items, expires = self._lists.get((ns, key), ([], None))
        if not self._live(expires):
            items = []
        items = (items + [json.dumps(value)])[-max_len:]
        self._lists[(ns, key)] = (items, self._expires(ttl_s) if ttl_s else expires)

    async def items(self, ns, key):
        items, expires = self._lists.get((ns, key), ([], None))
        if not self._live(expires):
            return []
        return [json.loads(x) for x in copy.copy(items)]


class RedisJourneyStore(JourneyStore):
    def __init__(self, redis):
        self.redis = redis              # careroute.storage.redis_client.RedisConnection

    def _k(self, ns, key):
        return self.redis.key("uk", ns, key)

    async def get(self, ns, key):
        raw = await self.redis.client.get(self._k(ns, key))
        return json.loads(raw) if raw else None

    async def put(self, ns, key, value, ttl_s=None):
        await self.redis.client.set(self._k(ns, key), json.dumps(value), ex=ttl_s or None)

    async def put_if_absent(self, ns, key, value, ttl_s=None):
        ok = await self.redis.client.set(self._k(ns, key), json.dumps(value),
                                         nx=True, ex=ttl_s or None)
        return bool(ok)

    async def delete(self, ns, key):
        await self.redis.client.delete(self._k(ns, key))

    async def append(self, ns, key, value, max_len=500, ttl_s=None):
        k = self._k(ns, key)
        pipe = self.redis.client.pipeline()
        pipe.rpush(k, json.dumps(value))
        pipe.ltrim(k, -max_len, -1)
        if ttl_s:
            pipe.expire(k, ttl_s)
        await pipe.execute()

    async def items(self, ns, key):
        return [json.loads(x) for x in await self.redis.client.lrange(self._k(ns, key), 0, -1)]
