"""实验平台单元测试：文档级命中指标、聚合、RAG_test 问题集解析、配置校验。"""

from __future__ import annotations

import json

from app.experiment.metrics import aggregate_results, doc_level_results, evaluate_docs
from app.experiment.runner import config_label, validate_config
from app.experiment.service import dedupe_parsed_questions, parse_question_file


class TestDocLevelResults:
    def test_dedupe_and_order(self):
        chunks = [
            {"filename": "b.md", "score": 0.9, "text": "bbb"},
            {"filename": "a.md", "score": 0.8, "text": "aaa"},
            {"filename": "b.md", "score": 0.7, "text": "bbb2"},
        ]
        docs = doc_level_results(chunks, top_k=5)
        assert [d["filename"] for d in docs] == ["b.md", "a.md"]
        assert docs[0]["rank"] == 1 and docs[1]["rank"] == 2
        assert docs[0]["score"] == 0.9  # 保留首现（最高）分数

    def test_top_k_truncation(self):
        chunks = [{"filename": f"f{i}.md", "score": 1.0 - i * 0.1, "text": ""} for i in range(10)]
        assert len(doc_level_results(chunks, top_k=3)) == 3

    def test_snippet_truncated(self):
        docs = doc_level_results([{"filename": "x.md", "score": 1.0, "text": "长" * 300}], top_k=5)
        assert len(docs[0]["snippet"]) <= 120


class TestEvaluateDocs:
    def test_hit_first_rank(self):
        docs = [{"filename": "gold.md", "rank": 1}, {"filename": "other.md", "rank": 2}]
        m = evaluate_docs(docs, ["gold.md"])
        assert m["hit"] is True and m["hit_rank"] == 1 and m["reciprocal_rank"] == 1.0

    def test_hit_later_rank(self):
        docs = [{"filename": "a.md", "rank": 1}, {"filename": "gold.md", "rank": 2}]
        m = evaluate_docs(docs, ["gold.md"])
        assert m["hit_rank"] == 2 and abs(m["reciprocal_rank"] - 0.5) < 1e-9

    def test_miss(self):
        m = evaluate_docs([{"filename": "a.md", "rank": 1}], ["gold.md"])
        assert m["hit"] is False and m["hit_rank"] == 0 and m["recall"] == 0.0

    def test_recall_multi_gold(self):
        docs = [{"filename": "g1.md", "rank": 1}, {"filename": "x.md", "rank": 2}, {"filename": "g2.md", "rank": 3}]
        m = evaluate_docs(docs, ["g1.md", "g2.md", "g3.md"])
        assert abs(m["recall"] - 2 / 3) < 1e-9

    def test_ood_empty_gold(self):
        m = evaluate_docs([{"filename": "a.md", "rank": 1}], [])
        assert m["hit"] is False and m["recall"] == 0.0


class TestAggregate:
    def test_aggregate_mixed(self):
        rows = [
            {"is_ood": False, "hit": True, "hit_rank": 1, "reciprocal_rank": 1.0, "recall": 1.0, "latency_ms": 100, "error": None},
            {"is_ood": False, "hit": False, "hit_rank": 0, "reciprocal_rank": 0.0, "recall": 0.0, "latency_ms": 200, "error": None},
            {"is_ood": True, "hit": False, "hit_rank": 0, "reciprocal_rank": 0.0, "recall": 0.0, "top1_score": 0.4, "rerank_below_min": True, "latency_ms": 50, "error": None},
            {"is_ood": True, "hit": False, "hit_rank": 0, "reciprocal_rank": 0.0, "recall": 0.0, "top1_score": 0.9, "rerank_below_min": False, "latency_ms": 50, "error": None},
        ]
        agg = aggregate_results(rows)
        assert agg["question_count"] == 2
        assert agg["hit_rate"] == 0.5 and agg["mrr"] == 0.5
        assert agg["ood"]["count"] == 2 and agg["ood"]["gated_rate"] == 0.5

    def test_empty(self):
        agg = aggregate_results([])
        assert agg["question_count"] == 0 and agg["hit_rate"] == 0.0
        assert agg["ood"]["count"] == 0 and agg["ood"]["gated_rate"] is None


class TestParseQuestionFile:
    def test_rag_test_1doc_format(self):
        payload = {
            "cases": [
                {"id": "x1", "question": "Q1?", "answer": "A1", "file_key": "documents/x1.md", "stratum": "numeric"},
                {"id": "x2", "question": "Q2?", "answer": "A2", "file_keys": ["x2_news1.md", "x2_news2.md"]},
            ]
        }
        parsed = parse_question_file(json.dumps(payload).encode("utf-8"))
        assert len(parsed) == 2
        assert parsed[0]["gold_file_keys"] == ["x1.md"]  # 路径取 basename
        assert parsed[1]["gold_file_keys"] == ["x2_news1.md", "x2_news2.md"]
        assert all(not p["is_ood"] for p in parsed)

    def test_ood_format(self):
        payload = {"cases": [{"id": "o1", "question": "Q?", "answer": "A", "document_in_pack": False}]}
        parsed = parse_question_file(json.dumps(payload).encode("utf-8"))
        assert parsed[0]["is_ood"] is True and parsed[0]["gold_file_keys"] == []

    def test_jsonl_and_bare_list(self):
        jsonl = b'{"question": "Q1?", "file_key": "a.md"}\n{"question": "Q2?", "file_key": "b.md"}\n'
        assert len(parse_question_file(jsonl)) == 2
        bare = json.dumps([{"question": "Q?", "document": "docs/c.md"}]).encode("utf-8")
        parsed = parse_question_file(bare)
        assert parsed[0]["gold_file_keys"] == ["c.md"]

    def test_invalid(self):
        for bad in (b"", b"{}", b'{"cases": []}'):
            try:
                parse_question_file(bad)
                raise AssertionError(f"should raise for {bad!r}")
            except ValueError:
                pass


class TestDedupeQuestions:
    @staticmethod
    def _p(question, is_ood=False):
        return {"ext_id": "", "question": question, "answer": "", "gold_file_keys": [], "stratum": "", "is_ood": is_ood}

    def test_skip_existing(self):
        kept, skipped = dedupe_parsed_questions([self._p("Q1"), self._p("Q2")], {"Q1"})
        assert [k["question"] for k in kept] == ["Q2"] and skipped == 1

    def test_dedupe_within_batch(self):
        kept, skipped = dedupe_parsed_questions([self._p("Q1"), self._p("Q1"), self._p("Q2")], set())
        assert [k["question"] for k in kept] == ["Q1", "Q2"] and skipped == 1

    def test_whitespace_insensitive(self):
        kept, skipped = dedupe_parsed_questions([self._p("  Q1  ")], {"Q1"})
        assert kept == [] and skipped == 1

    def test_nothing_to_skip(self):
        kept, skipped = dedupe_parsed_questions([self._p("Q1")], set())
        assert len(kept) == 1 and skipped == 0


class TestValidateConfig:
    def test_defaults_and_label(self):
        cfg = validate_config({"retrieval_mode": "hybrid"})
        assert cfg["fusion"] == "rrf" and cfg["rerank"] is True and cfg["top_k"] == 5
        assert config_label(cfg) == "hybrid+rrf+rerank"

    def test_sparse_no_rerank_label(self):
        cfg = validate_config({"retrieval_mode": "sparse", "rerank": False})
        assert config_label(cfg) == "sparse"

    def test_clamping(self):
        cfg = validate_config({"retrieval_mode": "dense", "top_k": 999, "rrf_k": 0, "candidate_multiplier": 99})
        assert cfg["top_k"] == 50 and cfg["rrf_k"] == 1 and cfg["candidate_multiplier"] == 10

    def test_invalid_mode(self):
        try:
            validate_config({"retrieval_mode": "magic"})
            raise AssertionError("should raise")
        except ValueError:
            pass

    def test_invalid_weighted_params(self):
        try:
            validate_config({"retrieval_mode": "hybrid", "weighted_params": [1]})
            raise AssertionError("should raise")
        except ValueError:
            pass
