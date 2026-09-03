"""LLM pinned 客户端注册表：同 base_url 复用、不同 base_url 隔离、关停清理。"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock


class PinnedClientCacheTests(unittest.TestCase):
    def setUp(self):
        from app.utils import egress

        egress._LLM_CLIENT_CACHE.clear()

    def tearDown(self):
        from app.utils import egress

        egress._LLM_CLIENT_CACHE.clear()

    def _patch_build(self, egress):
        """避免真实 DNS/建连：桩掉底层客户端构造。"""
        calls = []

        def _fake_build(base_url, timeout=None):
            calls.append(base_url)
            return (mock.MagicMock(name="sync"), mock.AsyncMock(name="async"))

        return mock.patch.object(egress, "build_pinned_clients", _fake_build), calls

    def test_same_base_url_reuses_pair(self):
        from app.utils import egress

        patch, calls = self._patch_build(egress)
        with patch:
            s1, a1 = egress.get_or_build_pinned_llm_clients("https://up.example/v1")
            s2, a2 = egress.get_or_build_pinned_llm_clients("https://up.example/v1")
        self.assertIs(s1, s2)
        self.assertIs(a1, a2)
        self.assertEqual(len(calls), 1)  # 只建一次

    def test_kwargs_returns_cached_clients(self):
        from app.utils import egress

        patch, calls = self._patch_build(egress)
        with patch:
            k1 = egress.pinned_llm_client_kwargs("https://up.example/v1")
            k2 = egress.pinned_llm_client_kwargs("https://up.example/v1")
        self.assertIs(k1["http_client"], k2["http_client"])
        self.assertIs(k1["http_async_client"], k2["http_async_client"])
        self.assertEqual(len(calls), 1)

    def test_empty_base_url_returns_empty(self):
        from app.utils import egress

        self.assertEqual(egress.pinned_llm_client_kwargs(None), {})
        self.assertEqual(egress.pinned_llm_client_kwargs(""), {})

    def test_different_base_urls_isolated(self):
        from app.utils import egress

        patch, calls = self._patch_build(egress)
        with patch:
            s1, _ = egress.get_or_build_pinned_llm_clients("https://a.example/v1")
            s2, _ = egress.get_or_build_pinned_llm_clients("https://b.example/v1")
        self.assertIsNot(s1, s2)
        self.assertEqual(len(calls), 2)

    def test_close_clears_cache_and_closes_clients(self):
        from app.utils import egress

        patch, _ = self._patch_build(egress)
        with patch:
            sync, async_c = egress.get_or_build_pinned_llm_clients("https://up.example/v1")

        asyncio.run(egress.close_pinned_llm_clients())
        sync.close.assert_called_once()
        async_c.aclose.assert_awaited_once()
        self.assertEqual(len(egress._LLM_CLIENT_CACHE), 0)

    def test_bounded_eviction(self):
        from app.utils import egress

        evicted = []
        patch, _ = self._patch_build(egress)
        with patch:
            for i in range(egress._LLM_CLIENT_CACHE_MAX + 2):
                s, _ = egress.get_or_build_pinned_llm_clients(f"https://up{i}.example/v1")
                evicted.append(s)
        self.assertEqual(len(egress._LLM_CLIENT_CACHE), egress._LLM_CLIENT_CACHE_MAX)
        # 最早插入的两个被逐出并关闭
        evicted[0].close.assert_called_once()
        evicted[1].close.assert_called_once()
        # 仍在缓存中的客户端不应被关闭
        evicted[-1].close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
