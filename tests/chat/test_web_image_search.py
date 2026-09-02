"""
文字搜图 web_image_search：博查解析、Markdown 格式化、本轮禁用守卫、前端不改写 https 图。
不打外网。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.chat.tools import (
    _set_last_rag_context,
    get_last_rag_context,
    reset_tool_call_guards,
    set_turn_tool_policy,
)
from app.chat.agent_service import _WEB_SEARCH_DISCIPLINE, _format_turn_context_block
from app.chat.web_search_tool import (
    _dedupe_images,
    _drop_already_shown_images,
    _format_image_search_output,
    _image_display_title,
    _merge_image_web_sources,
    _normalize_image_url,
    _parse_bocha_images,
    _prepare_ranked_images,
    _rank_image_results,
    _rerank_web_images,
    _sanitize_md_alt,
    make_web_image_search_tool,
)
from app.settings import settings

_WEB_MARKDOWN_JS = (
    Path(__file__).resolve().parents[2] / "web" / "src" / "utils" / "agentChatMarkdown.js"
)


def _bocha_payload() -> dict:
    return {
        "code": 200,
        "data": {
            "images": {
                "value": [
                    {
                        "name": "橘猫",
                        "contentUrl": "https://cdn.example/cat.jpg",
                        "hostPageUrl": "https://example.com/cat",
                        "width": 800,
                        "height": 600,
                    },
                    {
                        "name": "明文图",
                        "contentUrl": "http://insecure.example/dog.jpg",
                        "hostPageUrl": "https://example.com/dog",
                    },
                    {
                        "title": "协议不对",
                        "contentUrl": "ftp://files.example/x.png",
                    },
                    {
                        "name": "缺地址",
                        "contentUrl": "",
                    },
                ]
            }
        },
    }


class ParseBochaImagesTests(unittest.TestCase):
    def test_keeps_https_and_drops_others(self):
        items = _parse_bocha_images(_bocha_payload())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "橘猫")
        self.assertEqual(items[0]["contentUrl"], "https://cdn.example/cat.jpg")
        self.assertEqual(items[0]["hostPageUrl"], "https://example.com/cat")
        self.assertEqual(items[0]["width"], 800)
        self.assertEqual(items[0]["height"], 600)

    def test_images_value_at_root(self):
        payload = {
            "images": {
                "value": [
                    {"name": "根级", "contentUrl": "https://cdn.example/root.webp"},
                ]
            }
        }
        items = _parse_bocha_images(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["contentUrl"], "https://cdn.example/root.webp")

    def test_non_dict_payload(self):
        self.assertEqual(_parse_bocha_images(None), [])
        self.assertEqual(_parse_bocha_images([]), [])

    def test_drops_loopback_and_markdown_breaking_urls(self):
        payload = {
            "images": {
                "value": [
                    {"name": "本机", "contentUrl": "https://127.0.0.1/x.jpg"},
                    {"name": "主机", "contentUrl": "https://localhost/x.jpg"},
                    {
                        "name": "破坏",
                        "contentUrl": "https://cdn.example/a.jpg)![x](https://evil.example/x.png",
                    },
                    {
                        "name": "好图",
                        "contentUrl": "https://cdn.example/ok.jpg",
                        "hostPageUrl": "https://127.0.0.1/admin",
                    },
                ]
            }
        }
        items = _parse_bocha_images(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "好图")
        self.assertEqual(items[0]["contentUrl"], "https://cdn.example/ok.jpg")
        self.assertEqual(items[0].get("hostPageUrl") or "", "")


class FormatImageSearchTests(unittest.TestCase):
    def test_markdown_count_and_https_lines(self):
        results = [
            {
                "title": f"图{i}",
                "contentUrl": f"https://cdn.example/{i}.jpg",
                "hostPageUrl": f"https://ex.com/{i}",
            }
            for i in range(1, 7)
        ]
        with mock.patch.object(settings, "WEB_IMAGE_SEARCH_MARKDOWN_COUNT", 4):
            text = _format_image_search_output(results)
        self.assertIn("![图1](https://cdn.example/1.jpg)", text)
        self.assertIn("![图4](https://cdn.example/4.jpg)", text)
        self.assertEqual(text.count("](https://"), 4)
        self.assertNotIn("![图5]", text)
        self.assertIn("[5] 图5", text)
        self.assertIn("来源页: https://ex.com/5", text)
        self.assertIn("只复制", text)

    def test_empty_title_uses_host_not_parens(self):
        results = [
            {
                "title": "",
                "contentUrl": "https://i0.hdslb.com/bfs/new_dyn/a.jpg",
                "hostPageUrl": "https://www.bilibili.com/opus/1",
            }
        ]
        text = _format_image_search_output(results)
        self.assertNotIn("(无标题)", text)
        self.assertIn("![bilibili.com](https://i0.hdslb.com/bfs/new_dyn/a.jpg)", text)
        self.assertEqual(_image_display_title(results[0]), "bilibili.com")
        self.assertEqual(_sanitize_md_alt("(无标题)"), "无标题")

    def test_empty_title_and_url_falls_back_to_tupian(self):
        self.assertEqual(_image_display_title({"title": "", "contentUrl": "", "hostPageUrl": ""}), "图片")

    def test_title_markdown_injection_stripped(self):
        results = [
            {
                "title": "橘猫\n![pwn](https://evil.example/x.png)",
                "contentUrl": "https://cdn.example/cat.jpg",
                "hostPageUrl": "https://example.com/cat",
            }
        ]
        text = _format_image_search_output(results)
        self.assertEqual(text.count("](https://"), 1)
        self.assertNotIn("![pwn]", text)
        self.assertIn("![", text)
        self.assertIn("https://cdn.example/cat.jpg", text)


class RankImageResultsTests(unittest.TestCase):
    def test_prefers_clean_source_over_aggregator(self):
        results = [
            {
                "title": "Pinterest 转载",
                "contentUrl": "https://cdn.example/pin.jpg",
                "hostPageUrl": "https://www.pinterest.com/pin/123",
                "width": 1200,
                "height": 900,
            },
            {
                "title": "维基百科",
                "contentUrl": "https://upload.wikimedia.org/cat.jpg",
                "hostPageUrl": "https://zh.wikipedia.org/wiki/%E7%8C%AB",
                "width": 800,
                "height": 600,
            },
        ]
        ranked = _rank_image_results("橘猫", results)
        self.assertEqual(ranked[0]["title"], "维基百科")

    def test_drops_aggregator_when_enough_clean(self):
        clean = [
            {
                "title": f"馆藏{i}",
                "contentUrl": f"https://cdn.example/{i}.jpg",
                "hostPageUrl": f"https://museum.example/item/{i}",
                "width": 1000,
                "height": 800,
            }
            for i in range(6)
        ]
        junk = [
            {
                "title": "Pinterest",
                "contentUrl": "https://cdn.example/pin.jpg",
                "hostPageUrl": "https://www.pinterest.com/pin/999",
                "width": 1600,
                "height": 1200,
            }
        ]
        with mock.patch.object(settings, "WEB_IMAGE_SEARCH_MAX_RESULTS", 6):
            ranked = _rank_image_results("展品", clean + junk)
        self.assertEqual(len(ranked), 6)
        self.assertTrue(
            all("pinterest.com" not in (x.get("hostPageUrl") or "") for x in ranked)
        )

    def test_drops_tiny_when_larger_exist(self):
        results = [
            {
                "title": "缩略图",
                "contentUrl": "https://cdn.example/tiny.jpg",
                "hostPageUrl": "https://museum.example/tiny",
                "width": 80,
                "height": 80,
            },
            {
                "title": "大图",
                "contentUrl": "https://cdn.example/big.jpg",
                "hostPageUrl": "https://museum.example/big",
                "width": 1600,
                "height": 1200,
            },
        ]
        with mock.patch.object(settings, "WEB_IMAGE_SEARCH_MIN_EDGE", 240):
            ranked = _rank_image_results("展品", results)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["title"], "大图")

    def test_keeps_tiny_if_all_tiny(self):
        results = [
            {
                "title": "小1",
                "contentUrl": "https://cdn.example/a.jpg",
                "hostPageUrl": "https://museum.example/a",
                "width": 64,
                "height": 64,
            },
            {
                "title": "小2",
                "contentUrl": "https://cdn.example/b.jpg",
                "hostPageUrl": "https://museum.example/b",
                "width": 80,
                "height": 80,
            },
        ]
        with mock.patch.object(settings, "WEB_IMAGE_SEARCH_MIN_EDGE", 240):
            ranked = _rank_image_results("展品", results)
        self.assertEqual(len(ranked), 2)

    def test_truncates_to_max_results(self):
        results = [
            {
                "title": f"图{i}",
                "contentUrl": f"https://cdn.example/{i}.jpg",
                "hostPageUrl": f"https://museum.example/{i}",
                "width": 800,
                "height": 600,
            }
            for i in range(12)
        ]
        with mock.patch.object(settings, "WEB_IMAGE_SEARCH_MAX_RESULTS", 6):
            ranked = _rank_image_results("展品", results)
        self.assertEqual(len(ranked), 6)

    def test_all_junk_still_returns(self):
        results = [
            {
                "title": "针1",
                "contentUrl": "https://i.pinimg.com/1.jpg",
                "hostPageUrl": "https://www.pinterest.com/pin/1",
                "width": 900,
                "height": 700,
            },
            {
                "title": "针2",
                "contentUrl": "https://i.pinimg.com/2.jpg",
                "hostPageUrl": "https://www.pinterest.com/pin/2",
                "width": 900,
                "height": 700,
            },
        ]
        ranked = _rank_image_results("橘猫", results)
        self.assertEqual(len(ranked), 2)


class ImageDedupeTests(unittest.TestCase):
    def tearDown(self):
        get_last_rag_context(clear=True)

    def test_query_variant_same_image(self):
        results = [
            {
                "title": "原图",
                "contentUrl": "https://CDN.example/a.jpg",
                "hostPageUrl": "https://ex.com/1",
            },
            {
                "title": "带尺寸",
                "contentUrl": "https://cdn.example/a.jpg?w=800&h=600",
                "hostPageUrl": "https://ex.com/2",
            },
        ]
        self.assertEqual(
            _normalize_image_url("https://cdn.example/a.jpg?w=800"),
            _normalize_image_url("https://CDN.example/a.jpg"),
        )
        deduped = _dedupe_images(results)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["title"], "原图")

    def test_drops_already_shown_from_rag_context(self):
        _set_last_rag_context(
            {
                "web_sources": [
                    {
                        "index": 1,
                        "title": "旧图",
                        "url": "https://ex.com/old",
                        "image_url": "https://cdn.example/a.jpg?w=200",
                        "content_type": "image",
                    }
                ]
            }
        )
        results = [
            {
                "title": "重复",
                "contentUrl": "https://cdn.example/a.jpg",
                "hostPageUrl": "https://ex.com/new",
                "width": 800,
                "height": 600,
            },
            {
                "title": "新图",
                "contentUrl": "https://cdn.example/b.jpg",
                "hostPageUrl": "https://ex.com/b",
                "width": 800,
                "height": 600,
            },
        ]
        left = _drop_already_shown_images(results)
        self.assertEqual([x["title"] for x in left], ["新图"])
        with mock.patch.object(settings, "WEB_IMAGE_RERANK_ENABLED", False):
            ranked = _prepare_ranked_images("猫", results)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["title"], "新图")

    def test_merge_skips_duplicate_image_chip(self):
        _set_last_rag_context(
            {
                "web_sources": [
                    {
                        "index": 1,
                        "title": "旧",
                        "url": "https://ex.com/a",
                        "image_url": "https://cdn.example/a.jpg",
                        "content_type": "image",
                    }
                ]
            }
        )
        _merge_image_web_sources(
            [
                {
                    "index": 1,
                    "title": "重复",
                    "url": "https://ex.com/a2",
                    "image_url": "https://cdn.example/a.jpg?w=800",
                    "content_type": "image",
                }
            ]
        )
        ctx = get_last_rag_context(clear=True)
        self.assertEqual(len(ctx["web_sources"]), 1)

    def test_discipline_forbids_repeat_copy(self):
        self.assertIn("同一图片地址不要再复制", _WEB_SEARCH_DISCIPLINE)

    def test_discipline_requires_copy_marked_line_only(self):
        self.assertIn("只复制工具标出的那一行", _WEB_SEARCH_DISCIPLINE)
        tool = make_web_image_search_tool()
        self.assertIn("只复制工具标出的那一行", tool.description)

    def test_discipline_requires_proper_names(self):
        self.assertIn("专名", _WEB_SEARCH_DISCIPLINE)
        self.assertIn("禁止使用「这个人物」", _WEB_SEARCH_DISCIPLINE)
        self.assertIn("extra_query", _WEB_SEARCH_DISCIPLINE)

    def test_tool_description_requires_proper_names(self):
        tool = make_web_image_search_tool()
        self.assertIn("专名", tool.description)
        self.assertIn("这个人物", tool.description)
        query_field = tool.args_schema.model_fields["query"]
        self.assertIn("专名", query_field.description)
        self.assertIn("这个人物", query_field.description)

    def test_turn_context_with_caption_hints_search_query(self):
        block = _format_turn_context_block(
            use_knowledge_retrieval=False,
            use_web_search=True,
            document_filter=None,
            session_attachment_hint="",
            memory_inject=None,
            mcp_approval_note=None,
            image_caption="角色：Alice Margatroid。外观标签：金发、红色发带。",
        )
        self.assertIn("Alice Margatroid", block)
        self.assertIn("web_image_search", block)
        self.assertIn("专名", block)
        self.assertIn("这个人物", block)
        self.assertIn("untrusted_external_content", block)

    def test_turn_context_caption_without_web_skips_search_hint(self):
        block = _format_turn_context_block(
            use_knowledge_retrieval=True,
            use_web_search=False,
            document_filter=None,
            session_attachment_hint="",
            memory_inject=None,
            mcp_approval_note=None,
            image_caption="角色：Alice Margatroid。",
        )
        self.assertIn("Alice Margatroid", block)
        self.assertNotIn("若需搜图", block)


class VlRerankTests(unittest.TestCase):
    def _vl_settings(self):
        return {
            "WEB_IMAGE_RERANK_ENABLED": True,
            "RERANK_MODEL": "qwen3-vl-rerank",
            "RERANK_API_KEY": "sk-test",
            "RERANK_BINDING_HOST": "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
            "RERANK_MAX_CANDIDATES": 30,
            "RERANK_TIMEOUT_SECONDS": 15,
        }

    def _patch_settings(self, **overrides):
        vals = {**self._vl_settings(), **overrides}
        return mock.patch.multiple(settings, **vals)

    def test_high_rerank_score_ranks_first(self):
        results = [
            {
                "title": "一般来源但图很准",
                "contentUrl": "https://cdn.example/match.jpg",
                "hostPageUrl": "https://blog.example/p/1",
                "width": 1000,
                "height": 800,
                "rerank_score": 0.95,
            },
            {
                "title": "维基",
                "contentUrl": "https://upload.wikimedia.org/off.jpg",
                "hostPageUrl": "https://zh.wikipedia.org/wiki/X",
                "width": 1000,
                "height": 800,
                "rerank_score": 0.1,
            },
        ]
        ranked = _rank_image_results("橘猫", results)
        self.assertEqual(ranked[0]["title"], "一般来源但图很准")

    def test_skips_without_api_key(self):
        results = [
            {
                "title": "猫",
                "contentUrl": "https://cdn.example/cat.jpg",
                "hostPageUrl": "https://museum.example/cat",
                "width": 800,
                "height": 600,
            }
        ]
        with self._patch_settings(RERANK_API_KEY=""):
            out, meta = _rerank_web_images("橘猫", results)
        self.assertTrue(meta.get("skipped"))
        self.assertFalse(meta.get("applied"))
        self.assertFalse(meta.get("fallback"))
        self.assertNotIn("rerank_score", out[0])

    def test_skips_non_vl_model(self):
        results = [
            {
                "title": "猫",
                "contentUrl": "https://cdn.example/cat.jpg",
                "hostPageUrl": "https://museum.example/cat",
                "width": 800,
                "height": 600,
            }
        ]
        with self._patch_settings(RERANK_MODEL="gte-rerank"):
            out, meta = _rerank_web_images("橘猫", results)
        self.assertEqual(meta.get("skipped"), "disabled")
        self.assertFalse(meta.get("applied"))

    def test_http_failure_is_open(self):
        results = [
            {
                "title": "猫",
                "contentUrl": "https://cdn.example/cat.jpg",
                "hostPageUrl": "https://museum.example/cat",
                "width": 800,
                "height": 600,
            }
        ]

        class _Resp:
            status_code = 500
            text = "boom"

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        with self._patch_settings(), mock.patch("httpx.Client", _Client):
            out, meta = _rerank_web_images("橘猫", results)
        self.assertTrue(meta.get("fallback"))
        self.assertFalse(meta.get("applied"))
        self.assertNotIn("rerank_score", out[0])

    def test_only_sends_https_content_urls(self):
        results = [
            {
                "title": "https图",
                "contentUrl": "https://cdn.example/ok.jpg",
                "hostPageUrl": "https://museum.example/ok",
                "width": 800,
                "height": 600,
            },
            {
                "title": "无地址",
                "contentUrl": "",
                "hostPageUrl": "https://museum.example/empty",
                "width": 800,
                "height": 600,
            },
        ]
        captured: dict = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"output": {"results": [{"index": 0, "relevance_score": 0.8}]}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None, **kwargs):
                captured["payload"] = json
                return _Resp()

        with self._patch_settings(), mock.patch("httpx.Client", _Client):
            out, meta = _rerank_web_images("橘猫", results)
        self.assertTrue(meta.get("applied"))
        docs = captured["payload"]["input"]["documents"]
        self.assertEqual(docs, [{"image": "https://cdn.example/ok.jpg"}])
        self.assertEqual(out[0]["rerank_score"], 0.8)
        self.assertNotIn("rerank_score", out[1])

    def test_prepare_http_failure_still_ranks(self):
        results = [
            {
                "title": "A",
                "contentUrl": "https://cdn.example/a.jpg",
                "hostPageUrl": "https://museum.example/a",
                "width": 800,
                "height": 600,
            }
        ]

        class _Resp:
            status_code = 503
            text = "down"

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        with self._patch_settings(), mock.patch("httpx.Client", _Client):
            ranked = _prepare_ranked_images("展品", results)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["title"], "A")

    def test_batch_download_error_retries_per_image(self):
        results = [
            {
                "title": "能拉到",
                "contentUrl": "https://cdn.example/ok.jpg",
                "hostPageUrl": "https://museum.example/ok",
                "width": 800,
                "height": 600,
            },
            {
                "title": "防盗链",
                "contentUrl": "https://i0.hdslb.com/bfs/bad.jpg",
                "hostPageUrl": "https://www.bilibili.com/x",
                "width": 800,
                "height": 600,
            },
        ]
        err_body = (
            '{"code":"InvalidParameter","message":'
            '"<400> InternalError.Algo.InvalidParameter: download form url error"}'
        )

        class _Err:
            status_code = 400
            text = err_body

        class _Ok:
            status_code = 200

            def json(self):
                return {"output": {"results": [{"index": 0, "relevance_score": 0.91}]}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None, **kwargs):
                docs = (json or {}).get("input", {}).get("documents") or []
                if len(docs) > 1:
                    return _Err()
                image = docs[0].get("image") or ""
                if image.endswith("ok.jpg"):
                    return _Ok()
                return _Err()

        with self._patch_settings(), mock.patch("httpx.Client", _Client):
            out, meta = _rerank_web_images("橘猫", results)
        self.assertTrue(meta.get("applied"))
        self.assertFalse(meta.get("fallback"))
        self.assertEqual(out[0]["rerank_score"], 0.91)
        self.assertNotIn("rerank_score", out[1])
        self.assertEqual(meta.get("skipped_bad"), 1)

    def test_per_image_retry_capped_at_three(self):
        results = [
            {
                "title": f"图{i}",
                "contentUrl": f"https://cdn.example/{i}.jpg",
                "hostPageUrl": f"https://museum.example/{i}",
                "width": 800,
                "height": 600,
            }
            for i in range(5)
        ]
        err_body = (
            '{"code":"InvalidParameter","message":'
            '"<400> InternalError.Algo.InvalidParameter: download form url error"}'
        )
        posts: list = []

        class _Err:
            status_code = 400
            text = err_body

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None, **kwargs):
                posts.append(json)
                return _Err()

        with self._patch_settings(), mock.patch("httpx.Client", _Client):
            out, meta = _rerank_web_images("橘猫", results)
        self.assertTrue(meta.get("fallback"))
        self.assertLessEqual(len(posts), 4)
        self.assertEqual(len(posts[0]["input"]["documents"]), 5)
        self.assertTrue(all(len((p.get("input") or {}).get("documents") or []) == 1 for p in posts[1:]))
        self.assertLessEqual(len(posts) - 1, 3)


class WebImageSearchGuardTests(unittest.TestCase):
    def setUp(self):
        reset_tool_call_guards()
        set_turn_tool_policy(use_knowledge_retrieval=False, use_web_search=False)

    def test_disabled_this_turn(self):
        tool = make_web_image_search_tool()
        out = tool.invoke({"query": "橘猫"})
        self.assertIn("TOOL_DISABLED_THIS_TURN", out)
        self.assertIn("web_image_search", out)

    def test_merges_existing_web_sources(self):
        set_turn_tool_policy(use_knowledge_retrieval=False, use_web_search=True)
        _set_last_rag_context(
            {
                "web_sources": [
                    {"index": 1, "title": "新闻", "url": "https://news.example/a"},
                    {"index": 2, "title": "百科", "url": "https://wiki.example/b"},
                ]
            }
        )
        _merge_image_web_sources(
            [
                {
                    "index": 1,
                    "title": "橘猫",
                    "url": "https://example.com/cat",
                    "image_url": "https://cdn.example/cat.jpg",
                    "content_type": "image",
                }
            ]
        )
        ctx = get_last_rag_context(clear=True)
        sources = ctx["web_sources"]
        self.assertEqual(len(sources), 3)
        self.assertEqual(sources[0]["title"], "新闻")
        self.assertEqual(sources[2]["content_type"], "image")
        self.assertEqual(sources[2]["index"], 3)
        self.assertEqual(sources[2]["image_url"], "https://cdn.example/cat.jpg")


class BuiltinToolNameTests(unittest.TestCase):
    def test_web_image_search_is_reserved(self):
        from app.mcp_client.service import _BUILTIN_TOOL_NAMES

        self.assertIn("web_image_search", _BUILTIN_TOOL_NAMES)
        self.assertIn("fetch_url", _BUILTIN_TOOL_NAMES)


class RewriteMediaUrlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.js_src = _WEB_MARKDOWN_JS.read_text(encoding="utf-8")

    def test_js_source_skips_https_images(self):
        self.assertIn("firstMediaImageUrl", self.js_src)
        self.assertIn("/^https:\\/\\//i.test(target)", self.js_src)
        self.assertIn("不要把 https:// 外链图改写成 /api/v1/media/", self.js_src)
        self.assertIn("referrerpolicy", self.js_src)
        self.assertIn("ADD_ATTR: ['referrerpolicy']", self.js_src)

    def test_rewrite_keeps_https_image(self):
        if not self.node:
            self.skipTest("需要 node 才能执行 rewriteMediaUrlsInText")
        text = "见图\n![橘猫](https://cdn.example/cat.jpg)\n完"
        sources = [
            {
                "image_url": (
                    "/api/v1/media/user_agent_images/kb.jpg?exp=1&sig=abc"
                )
            }
        ]
        out = _rewrite_media_urls_via_node(text, sources)
        self.assertIn("![橘猫](https://cdn.example/cat.jpg)", out)
        self.assertNotIn("/api/v1/media/", out)

    def test_rewrite_still_fixes_broken_kb_image(self):
        if not self.node:
            self.skipTest("需要 node 才能执行 rewriteMediaUrlsInText")
        fallback = "/api/v1/media/user_agent_images/kb.jpg?exp=1&sig=abc"
        text = "见图\n![知识库](stored/relpath.jpg)\n完"
        out = _rewrite_media_urls_via_node(text, [{"image_url": fallback}])
        self.assertIn(f"![知识库]({fallback})", out)
        self.assertNotIn("stored/relpath.jpg", out)

    def test_safe_chat_url_rejects_loopback(self):
        if not self.node:
            self.skipTest("需要 node 才能执行 isSafeChatUrl")
        self.assertTrue(_is_safe_chat_url_via_node("https://cdn.example/cat.jpg"))
        self.assertFalse(_is_safe_chat_url_via_node("https://127.0.0.1/x.jpg"))
        self.assertFalse(_is_safe_chat_url_via_node("https://localhost/x.jpg"))
        self.assertTrue(
            _is_safe_chat_url_via_node("/api/v1/media/user_agent_images/kb.jpg?exp=1&sig=abc")
        )


def _extract_markdown_js_helpers() -> str:
    src = _WEB_MARKDOWN_JS.read_text(encoding="utf-8")
    start = src.index("const MEDIA_PATH_RE")
    end = src.index("export function renderAgentChatMarkdown")
    return src[start:end].replace("export function ", "function ")


def _rewrite_media_urls_via_node(text: str, sources: list) -> str:
    extracted = _extract_markdown_js_helpers()
    harness = (
        extracted
        + "\nconst fs = require('fs');\n"
        + "const input = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        + "process.stdout.write(String(rewriteMediaUrlsInText(input.text, input.sources)));\n"
    )
    node = shutil.which("node")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "rewrite.cjs"
        script.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            [node, str(script)],
            input=json.dumps({"text": text, "sources": sources}, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    if proc.returncode != 0:
        raise AssertionError(f"node rewrite failed: {proc.stderr}\n{proc.stdout}")
    return proc.stdout


def _is_safe_chat_url_via_node(url: str) -> bool:
    extracted = _extract_markdown_js_helpers()
    harness = (
        extracted
        + "\nconst fs = require('fs');\n"
        + "const input = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        + "process.stdout.write(isSafeChatUrl(input.url) ? '1' : '0');\n"
    )
    node = shutil.which("node")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "safeurl.cjs"
        script.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            [node, str(script)],
            input=json.dumps({"url": url}, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    if proc.returncode != 0:
        raise AssertionError(f"node isSafeChatUrl failed: {proc.stderr}\n{proc.stdout}")
    return proc.stdout.strip() == "1"


if __name__ == "__main__":
    unittest.main()
