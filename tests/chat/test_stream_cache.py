"""Redis Stream 原语：限深入队、消费者组读取、XACK+XDEL 确认、深度。不依赖真实 Redis。"""

from __future__ import annotations

import unittest

from app.chat.cache import RedisCache


class _FakeStreamRedis:
    """最小 Stream 桩：覆盖 cache 使用的 xadd/xreadgroup/xack/xdel/xautoclaim/xpending/xgroup/xlen。

    eval 仅按脚本特征分派两种 Lua：限深入队、XACK+XDEL。
    """

    def __init__(self):
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.groups: dict[tuple[str, str], dict] = {}
        self._seq = 0

    def _new_id(self) -> str:
        self._seq += 1
        return f"{self._seq}-0"

    def xgroup_create(self, key, group, id, mkstream=False):
        if (key, group) in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.streams.setdefault(key, [])
        self.groups[(key, group)] = {"last": id, "pel": {}}
        return True

    def xadd(self, key, fields, maxlen=None, approximate=True, id="*"):
        mid = self._new_id()
        self.streams.setdefault(key, []).append((mid, dict(fields)))
        return mid

    def xlen(self, key):
        return len(self.streams.get(key, []))

    def xreadgroup(self, group, consumer, streams, count=None, block=None):
        out = []
        for key, _start in streams.items():
            g = self.groups[(key, group)]
            entries = self.streams.get(key, [])
            last = g.get("last", "0")
            if last in ("0", "0-0"):
                idx = 0
            else:
                idx = next(
                    (i + 1 for i, (mid, _f) in enumerate(entries) if mid == last), len(entries)
                )
            ready = entries[idx:]
            if count:
                ready = ready[:count]
            got = list(ready)
            for mid, _f in got:
                prev = g["pel"].get(mid, {}).get("delivery", 0)
                g["pel"][mid] = {"consumer": consumer, "delivery": prev + 1, "idle": 0}
                g["last"] = mid
            if got:
                out.append([key, got])
        return out or None

    def xack(self, key, group, *ids):
        pel = self.groups[(key, group)].get("pel", {})
        n = 0
        for mid in ids:
            if mid in pel:
                del pel[mid]
                n += 1
        return n

    def xdel(self, key, *ids):
        drop = set(ids)
        entries = self.streams.get(key, [])
        self.streams[key] = [(m, f) for m, f in entries if m not in drop]
        return len(drop)

    def xautoclaim(self, key, group, consumer, min_idle_time, start_id="0-0", count=None, justid=False):
        pel = self.groups[(key, group)].get("pel", {})
        claimed = []
        for mid, info in list(pel.items()):
            if info.get("idle", 0) >= int(min_idle_time):
                info["consumer"] = consumer
                info["delivery"] = info.get("delivery", 1) + 1
                info["idle"] = 0
                fields = next((f for m, f in self.streams.get(key, []) if m == mid), {})
                claimed.append((mid, fields))
        return ["0-0", claimed, []]

    def xpending(self, key, group):
        # 与 redis-py 7.x 一致：摘要为 dict {"pending": n, ...}
        pel = self.groups[(key, group)].get("pel", {})
        return {"pending": len(pel), "min": None, "max": None, "consumers": []}

    def xpending_range(self, key, group, min, max, count, consumername=None, idle=None):
        # 与 redis-py 7.x 一致：明细为 dict 行（含 times_delivered）
        pel = self.groups[(key, group)].get("pel", {})
        rows = [
            {
                "message_id": mid,
                "consumer": info["consumer"],
                "time_since_delivered": info.get("idle", 0),
                "times_delivered": info.get("delivery", 1),
            }
            for mid, info in pel.items()
            if (min == max and mid == min) or (min != max and min <= mid <= max)
        ]
        if count:
            rows = rows[:count]
        return rows

    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if "XLEN" in script and "XADD" in script:
            key, payload, maxd = keys[0], argv[0], int(argv[1])
            if maxd > 0 and self.xlen(key) >= maxd:
                return None
            return self.xadd(key, {"payload": payload})
        if "XACK" in script and "XDEL" in script:
            key, group, mid = keys[0], argv[0], argv[1]
            n = self.xack(key, group, mid)
            self.xdel(key, mid)
            return n
        return 0


class StreamPrimitiveTests(unittest.TestCase):
    def _cache(self) -> RedisCache:
        c = RedisCache()
        c._client = _FakeStreamRedis()  # type: ignore[attr-defined]
        return c

    def test_xadd_limited_respects_max_depth(self):
        c = self._cache()
        self.assertIsNotNone(c.xadd_limited_json("s", {"task_id": "a"}, 2))
        self.assertIsNotNone(c.xadd_limited_json("s", {"task_id": "b"}, 2))
        self.assertIsNone(c.xadd_limited_json("s", {"task_id": "c"}, 2))
        self.assertEqual(c.xlen("s"), 2)

    def test_xadd_limited_zero_is_unbounded(self):
        c = self._cache()
        for i in range(3):
            self.assertIsNotNone(c.xadd_limited_json("s", {"task_id": str(i)}, 0))

    def test_xreadgroup_parses_payload(self):
        c = self._cache()
        c.xgroup_create("s", "g", start="0")
        c.xadd_json("s", {"kind": "kb_upload", "task_id": "t1"})
        got = c.xreadgroup_json("s", "g", "c1", block_ms=10, count=1)
        self.assertEqual(got[0][1]["task_id"], "t1")

    def test_xack_del_removes_from_pending_and_stream(self):
        c = self._cache()
        c.xgroup_create("s", "g", start="0")
        c.xadd_json("s", {"task_id": "t1"})
        mid, _payload = c.xreadgroup_json("s", "g", "c1", block_ms=10, count=1)[0]
        self.assertEqual(c.xpending_count("s", "g"), 1)
        c.xack_del("s", "g", mid)
        self.assertEqual(c.xpending_count("s", "g"), 0)
        self.assertEqual(c.xlen("s"), 0)

    def test_xpending_delivery_count(self):
        c = self._cache()
        c.xgroup_create("s", "g", start="0")
        c.xadd_json("s", {"task_id": "t1"})
        mid, _payload = c.xreadgroup_json("s", "g", "c1", block_ms=10, count=1)[0]
        self.assertEqual(c.xpending_delivery_count("s", "g", mid), 1)

    def test_xgroup_create_tolerates_busygroup(self):
        c = self._cache()
        self.assertTrue(c.xgroup_create("s", "g", start="0"))
        self.assertTrue(c.xgroup_create("s", "g", start="0"))

    def test_xautoclaim_returns_claimed_payload(self):
        c = self._cache()
        c.xgroup_create("s", "g", start="0")
        c.xadd_json("s", {"task_id": "t1"})
        c.xreadgroup_json("s", "g", "dead", block_ms=10, count=1)
        claimed = c.xautoclaim_json("s", "g", "c2", min_idle_ms=0)
        self.assertEqual(claimed[0][1]["task_id"], "t1")


if __name__ == "__main__":
    unittest.main()
