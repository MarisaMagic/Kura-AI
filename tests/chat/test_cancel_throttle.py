"""取消检查节流：chunk 级探测仅在到点时实查，取消态不可逆。不依赖 Redis/网络。"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.chat import agent_service
from app.chat.agent_service import _throttled_cancel_check


class CancelThrottleTests(unittest.TestCase):
    def _run_probe(self, probe, n):
        async def _go():
            return [await probe() for _ in range(n)]

        return asyncio.run(_go())

    def test_none_input_returns_none(self):
        self.assertIsNone(_throttled_cancel_check(None))

    def test_real_check_at_most_once_per_interval(self):
        calls = {"n": 0}

        def _check():
            calls["n"] += 1
            return False

        clock = {"t": 100.0}
        probe = _throttled_cancel_check(_check)
        with mock.patch.object(agent_service.time, "monotonic", lambda: clock["t"]):
            self.assertEqual(self._run_probe(probe, 1), [False])  # t=100 实查
            self.assertEqual(self._run_probe(probe, 5), [False] * 5)  # 0.25s 内不再实查
            self.assertEqual(calls["n"], 1)

            clock["t"] = 100.26
            self.assertEqual(self._run_probe(probe, 1), [False])  # 到点再查
            self.assertEqual(calls["n"], 2)

    def test_flag_sticky_after_cancel(self):
        seq = iter([False, True])

        def _check():
            return next(seq, True)

        clock = {"t": 100.0}
        probe = _throttled_cancel_check(_check)
        with mock.patch.object(agent_service.time, "monotonic", lambda: clock["t"]):
            self.assertEqual(self._run_probe(probe, 1), [False])
            clock["t"] = 100.3
            self.assertEqual(self._run_probe(probe, 1), [True])
            clock["t"] = 100.31  # 未到间隔也直接返回 True（不可逆）
            self.assertEqual(self._run_probe(probe, 1), [True])

    def test_check_exception_is_swallowed(self):
        def _check():
            raise RuntimeError("redis down")

        clock = {"t": 100.0}
        probe = _throttled_cancel_check(_check)
        with mock.patch.object(agent_service.time, "monotonic", lambda: clock["t"]):
            self.assertEqual(self._run_probe(probe, 1), [False])
            clock["t"] = 100.3
            self.assertEqual(self._run_probe(probe, 1), [False])


if __name__ == "__main__":
    unittest.main()
