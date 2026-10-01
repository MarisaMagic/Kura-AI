"""异步出站客户端缓存：同 loop+host 复用、跨 loop 隔离、关停清理、上限驱逐。

覆盖 P2 新增的 egress._ASYNC_HTTP_CLIENT_CACHE 与 providers._ASYNC_CLIENTS。
不依赖真实网络：桩掉底层客户端构造。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock


class PinnedAsyncClientCacheTests(unittest.TestCase):
    def setUp(self):
        from app.utils import egress

        egress._ASYNC_HTTP_CLIENT_CACHE.clear()

    def tearDown(self):
        from app.utils import egress

        egress._ASYNC_HTTP_CLIENT_CACHE.clear()

    def _patch_build(self, egress):
        """避免真实 DNS/建连：桩掉底层异步客户端构造。"""
        calls = []

        def _fake_build(url, timeout=None):
            calls.append(url)
            return mock.AsyncMock(name=f"async_http_{len(calls)}")

        return mock.patch.object(egress, "build_pinned_async_client", _fake_build), calls

    def test_same_loop_same_host_reuses(self):
        from app.utils import egress

        patch, calls = self._patch_build(egress)

        async def _run():
            c1 = egress.get_or_build_pinned_async_client("https://a.example/page")
            c2 = egress.get_or_build_pinned_async_client("https://a.example/other")
            return c1, c2

        with patch:
            c1, c2 = asyncio.run(_run())
        self.assertIs(c1, c2)
        self.assertEqual(len(calls), 1)  # 同 host 只建一次

    def test_different_hosts_isolated(self):
        from app.utils import egress

        patch, calls = self._patch_build(egress)

        async def _run():
            return (
                egress.get_or_build_pinned_async_client("https://a.example/"),
                egress.get_or_build_pinned_async_client("https://b.example/"),
            )

        with patch:
            c1, c2 = asyncio.run(_run())
        self.assertIsNot(c1, c2)
        self.assertEqual(len(calls), 2)

    def test_cross_event_loop_isolated(self):
        """AsyncClient 与 loop 绑定：跨 loop 不得复用（回归 "Event loop is closed"）。"""
        from app.utils import egress

        patch, calls = self._patch_build(egress)

        def _get():
            async def _run():
                return egress.get_or_build_pinned_async_client("https://a.example/")

            return asyncio.run(_run())

        with patch:
            c1 = _get()
            c2 = _get()
        self.assertIsNot(c1, c2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(egress._ASYNC_HTTP_CLIENT_CACHE), 2)

    def test_close_clears_and_acloses(self):
        from app.utils import egress

        patch, _ = self._patch_build(egress)

        async def _run():
            return egress.get_or_build_pinned_async_client("https://a.example/")

        with patch:
            client = asyncio.run(_run())
        asyncio.run(egress.close_pinned_async_http_clients())
        client.aclose.assert_awaited_once()
        self.assertEqual(len(egress._ASYNC_HTTP_CLIENT_CACHE), 0)

    def test_bounded_eviction(self):
        from app.utils import egress

        patch, _ = self._patch_build(egress)

        async def _run():
            clients = []
            for i in range(egress._ASYNC_HTTP_CLIENT_CACHE_MAX + 2):
                clients.append(egress.get_or_build_pinned_async_client(f"https://h{i}.example/"))
            return clients

        with patch:
            clients = asyncio.run(_run())
        self.assertEqual(len(egress._ASYNC_HTTP_CLIENT_CACHE), egress._ASYNC_HTTP_CLIENT_CACHE_MAX)
        # 最后插入的仍在缓存中且未被关闭（淘汰项交给 GC，不主动 aclose）
        clients[-1].aclose.assert_not_called()


class SharedAsyncSearchClientTests(unittest.TestCase):
    def setUp(self):
        from app.chat import web_search_providers as wsp

        wsp._ASYNC_CLIENTS.clear()

    def tearDown(self):
        from app.chat import web_search_providers as wsp

        wsp._ASYNC_CLIENTS.clear()

    def _patch_httpx_client(self):
        created = []

        def _fake_client(**kwargs):
            client = mock.AsyncMock(name=f"shared_{len(created)}")
            created.append(client)
            return client

        return mock.patch("httpx.AsyncClient", new=mock.MagicMock(side_effect=_fake_client)), created

    def test_same_loop_same_key_reuses(self):
        from app.chat import web_search_providers as wsp

        patch, created = self._patch_httpx_client()

        async def _run():
            c1 = wsp._shared_async_client("bocha:https://api.example/v1", proxy=None)
            c2 = wsp._shared_async_client("bocha:https://api.example/v1", proxy=None)
            return c1, c2

        with patch:
            c1, c2 = asyncio.run(_run())
        self.assertIs(c1, c2)
        self.assertEqual(len(created), 1)

    def test_proxy_variants_isolated(self):
        from app.chat import web_search_providers as wsp

        patch, created = self._patch_httpx_client()

        async def _run():
            c1 = wsp._shared_async_client("bing_html", proxy=None)
            c2 = wsp._shared_async_client("bing_html", proxy="http://127.0.0.1:7890")
            return c1, c2

        with patch:
            c1, c2 = asyncio.run(_run())
        self.assertIsNot(c1, c2)
        self.assertEqual(len(created), 2)

    def test_cross_event_loop_isolated(self):
        from app.chat import web_search_providers as wsp

        patch, created = self._patch_httpx_client()

        def _get():
            async def _run():
                return wsp._shared_async_client("bocha:x", proxy=None)

            return asyncio.run(_run())

        with patch:
            c1 = _get()
            c2 = _get()
        self.assertIsNot(c1, c2)
        self.assertEqual(len(created), 2)
        self.assertEqual(len(wsp._ASYNC_CLIENTS), 2)

    def test_close_clears_and_acloses(self):
        from app.chat import web_search_providers as wsp

        patch, _ = self._patch_httpx_client()

        async def _run():
            return wsp._shared_async_client("bocha:x", proxy=None)

        with patch:
            client = asyncio.run(_run())
        asyncio.run(wsp.close_async_search_clients())
        client.aclose.assert_awaited_once()
        self.assertEqual(len(wsp._ASYNC_CLIENTS), 0)


if __name__ == "__main__":
    unittest.main()