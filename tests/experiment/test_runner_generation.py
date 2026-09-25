"""实验端到端评测集成测试：doc_context_map、evaluate_answer 分支与 runner 接入（mock LLM/Milvus）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.experiment import answer_eval
from app.experiment.runner import doc_context_map, run_config_for_question, validate_config

ANSWER_CFG = {"api_key": "fake", "base_url": "http://fake", "model_name": "fake-model"}
JUDGE_CFG = dict(ANSWER_CFG)


class _FakeModel:
    def __init__(self, responder, calls):
        self._responder = responder
        self._calls = calls

    def invoke(self, messages):
        prompt = messages[-1].content
        self._calls.append(prompt)
        return SimpleNamespace(content=self._responder(prompt))


def _patch_models(monkeypatch, responder):
    calls: list[str] = []
    monkeypatch.setattr(answer_eval, "_chat_model", lambda cfg: _FakeModel(responder, calls))
    return calls


def _is_correctness(prompt: str) -> bool:
    return "你是答案评审" in prompt


def _is_faithfulness(prompt: str) -> bool:
    return "你是事实核查员" in prompt


def _is_refusal(prompt: str) -> bool:
    return "是否明确表示" in prompt


class TestDocContextMap:
    def test_merge_by_file_and_topk(self):
        chunks = [
            {"filename": "a.md", "text": "甲" * 10, "page_number": 1, "content_type": "text", "score": 0.9},
            {"filename": "a.md", "text": "乙" * 10, "page_number": 2, "content_type": "text", "score": 0.8},
            {"filename": "b.md", "text": "丙" * 10, "page_number": 3, "content_type": "text", "score": 0.7},
            {"filename": "c.md", "text": "丁" * 10, "page_number": 4, "content_type": "text", "score": 0.6},
        ]
        ctx = doc_context_map(chunks, top_k=2, max_chars=1000)
        assert "[1] a.md" in ctx and "乙" in ctx
        assert "[2] b.md" in ctx
        assert "c.md" not in ctx

    def test_truncate(self):
        chunks = [{"filename": "a.md", "text": "甲" * 100, "page_number": 1, "content_type": "text"}]
        assert len(doc_context_map(chunks, top_k=5, max_chars=60)) <= 60

    def test_skip_image_blocks(self):
        chunks = [
            {"filename": "a.md", "text": "", "page_number": 1, "content_type": "image"},
            {"filename": "b.md", "text": "正文", "page_number": 2, "content_type": "text"},
        ]
        ctx = doc_context_map(chunks, top_k=5, max_chars=500)
        assert "[1] b.md" in ctx and "a.md" not in ctx


class TestEvaluateAnswer:
    def test_in_kb_all_metrics(self, monkeypatch):
        def responder(prompt):
            if _is_correctness(prompt):
                return (
                    '{"verdict": 0, "covered": ["营收1492.08亿"], "missing": ["同比增速11%"], '
                    '"conflicts": [], "reason": "缺少同比增速"}'
                )
            if _is_faithfulness(prompt):
                return '```json\n{"score": 1, "claims": 2, "supported": 2, "unsupported": []}\n```'
            return "腾讯2021年Q2营收1492.08亿元，同比增长11%。"

        calls = _patch_models(monkeypatch, responder)
        out = answer_eval.evaluate_answer(
            question="腾讯2021Q2营收和增速？",
            reference="营收1492.08亿元，同比增长11%",
            context="[1] news.md (Page 1):\n腾讯2021年Q2营收1492.08亿元，同比增长11%",
            is_ood=False,
            answer_cfg=ANSWER_CFG,
            judge_cfg=JUDGE_CFG,
        )
        m = out["answer_metrics"]
        assert out["answer"].startswith("腾讯")
        assert out["answer_latency_ms"] >= 0
        assert m["correctness"] == 0.0 and m["correctness_reason"] == "缺少同比增速"
        assert m["correctness_covered"] == ["营收1492.08亿"]
        assert m["correctness_missing"] == ["同比增速11%"]
        assert m["correctness_conflicts"] == []
        assert m["faithfulness"] == 1.0
        assert m["numeric_hit"] == 1.0
        assert "judge_error" not in m
        assert len(calls) == 3  # 生成 + 正确率 + 忠实度

    def test_correctness_verdict_mapping(self, monkeypatch):
        def run(verdict):
            def responder(prompt):
                if _is_correctness(prompt):
                    return f'{{"verdict": {verdict}}}'
                if _is_faithfulness(prompt):
                    return '{"score": 1}'
                return "答案"

            return _patch_models(monkeypatch, responder)

        for verdict, expected in ((1, 1.0), (0, 0.0)):
            run(verdict)
            out = answer_eval.evaluate_answer(
                question="q", reference="r", context="c", is_ood=False,
                answer_cfg=ANSWER_CFG, judge_cfg=JUDGE_CFG,
            )
            assert out["answer_metrics"]["correctness"] == expected

    def test_correctness_score_fallback_strict(self, monkeypatch):
        """兼容旧 score 字段：仅 1 算正确，0.5/0.6 等中间值按错误。"""

        def run(judge_score):
            def responder(prompt):
                if _is_correctness(prompt):
                    return f'{{"score": {judge_score}}}'
                if _is_faithfulness(prompt):
                    return '{"score": 1}'
                return "答案"

            return _patch_models(monkeypatch, responder)

        for judge_score, expected in ((1, 1.0), (0.6, 0.0), (0.5, 0.0), (0.0, 0.0)):
            run(judge_score)
            out = answer_eval.evaluate_answer(
                question="q", reference="r", context="c", is_ood=False,
                answer_cfg=ANSWER_CFG, judge_cfg=JUDGE_CFG,
            )
            assert out["answer_metrics"]["correctness"] == expected

    def test_correctness_judge_receives_context(self, monkeypatch):
        """正确率判分必须携带检索资料，用于核验额外信息与数字口径。"""

        def responder(prompt):
            if _is_correctness(prompt):
                return '{"verdict": 1}'
            if _is_faithfulness(prompt):
                return '{"score": 1}'
            return "答案"

        calls = _patch_models(monkeypatch, responder)
        answer_eval.evaluate_answer(
            question="q",
            reference="r",
            context="[1] news.md (Page 1):\n资料中的关键事实",
            is_ood=False,
            answer_cfg=ANSWER_CFG,
            judge_cfg=JUDGE_CFG,
        )
        correctness_prompt = next(p for p in calls if _is_correctness(p))
        assert "<<<资料开始>>>" in correctness_prompt
        assert "资料中的关键事实" in correctness_prompt

    def test_on_stage_order_in_kb(self, monkeypatch):
        def responder(prompt):
            if _is_correctness(prompt):
                return '{"score": 1}'
            if _is_faithfulness(prompt):
                return '{"score": 1}'
            return "答案"

        _patch_models(monkeypatch, responder)
        stages: list[tuple[str, int]] = []
        answer_eval.evaluate_answer(
            question="q", reference="r", context="c", is_ood=False,
            answer_cfg=ANSWER_CFG, judge_cfg=JUDGE_CFG,
            on_stage=lambda text, units: stages.append((text, units)),
        )
        assert stages == [("生成答案", 1), ("判分（正确率+忠实度）", 2)]

    def test_on_stage_order_ood(self, monkeypatch):
        def responder(prompt):
            if _is_refusal(prompt):
                return '{"refused": true}'
            return "拒绝回答"

        _patch_models(monkeypatch, responder)
        stages: list[tuple[str, int]] = []
        answer_eval.evaluate_answer(
            question="q", reference="", context="", is_ood=True,
            answer_cfg=ANSWER_CFG, judge_cfg=JUDGE_CFG,
            on_stage=lambda text, units: stages.append((text, units)),
        )
        assert stages == [("生成答案", 1), ("判分（拒答）", 1)]

    def test_on_stage_exception_propagates(self, monkeypatch):
        def responder(prompt):
            return "答案"

        _patch_models(monkeypatch, responder)

        def boom(text, units):
            raise RuntimeError("cancelled")

        with pytest.raises(RuntimeError):
            answer_eval.evaluate_answer(
                question="q", reference="r", context="c", is_ood=False,
                answer_cfg=ANSWER_CFG, judge_cfg=JUDGE_CFG, on_stage=boom,
            )

    def test_ood_only_refusal_judge(self, monkeypatch):
        def responder(prompt):
            if _is_refusal(prompt):
                return '{"refused": true}'
            return "根据现有资料无法回答。"

        calls = _patch_models(monkeypatch, responder)
        out = answer_eval.evaluate_answer(
            question="库外问题",
            reference="",
            context="",
            is_ood=True,
            answer_cfg=ANSWER_CFG,
            judge_cfg=JUDGE_CFG,
        )
        m = out["answer_metrics"]
        assert m["refused"] is True
        assert "correctness" not in m and "faithfulness" not in m
        assert len(calls) == 2  # 生成 + 拒答判分（不做正确率/忠实度）

    def test_generation_failure(self, monkeypatch):
        def responder(prompt):
            raise RuntimeError("upstream 500")

        _patch_models(monkeypatch, responder)
        out = answer_eval.evaluate_answer(
            question="q", reference="r", context="c", is_ood=False,
            answer_cfg=ANSWER_CFG, judge_cfg=JUDGE_CFG,
        )
        assert out["answer"] == ""
        assert "generate_failed" in out["answer_metrics"]["gen_error"]

    def test_judge_malformed_marks_error(self, monkeypatch):
        def responder(prompt):
            if _is_correctness(prompt):
                return "我觉得答案还不错"  # 非 JSON
            if _is_faithfulness(prompt):
                return '{"score": 1}'
            return "答案正文"

        _patch_models(monkeypatch, responder)
        out = answer_eval.evaluate_answer(
            question="q", reference="r", context="c", is_ood=False,
            answer_cfg=ANSWER_CFG, judge_cfg=JUDGE_CFG,
        )
        m = out["answer_metrics"]
        assert m.get("correctness") is None
        assert m["judge_error"] == "judge_no_json"
        assert m["faithfulness"] == 1.0


class _FakeMilvus:
    def dense_retrieve(self, dense_embedding, top_k, filter_expr):
        return [
            {
                "filename": "gold.md",
                "text": "营收1492.08亿元，同比增长11%。",
                "score": 0.9,
                "page_number": 1,
                "content_type": "text",
            }
        ]


class TestRunnerGeneration:
    def test_generate_flag_off_keeps_shape(self):
        cfg = validate_config({"retrieval_mode": "dense", "rerank": False, "top_k": 3})
        res = run_config_for_question(
            query="q", gold_file_keys=["gold.md"], config=cfg, dense_embedding=[0.1],
            milvus=_FakeMilvus(), kb_scope="exp:d1",
        )
        assert res["hit"] is True
        assert "answer" not in res and "answer_metrics" not in res

    def test_generate_flag_on(self, monkeypatch):
        def responder(prompt):
            if _is_correctness(prompt):
                return '{"score": 1, "reason": "一致"}'
            if _is_faithfulness(prompt):
                return '{"score": 1, "claims": 1, "supported": 1, "unsupported": []}'
            return "营收1492.08亿元，同比增长11%。"

        _patch_models(monkeypatch, responder)
        cfg = validate_config({"retrieval_mode": "dense", "rerank": False, "top_k": 3})
        res = run_config_for_question(
            query="营收多少？", gold_file_keys=["gold.md"], config=cfg, dense_embedding=[0.1],
            milvus=_FakeMilvus(), kb_scope="exp:d1",
            generate=True,
            reference_answer="营收1492.08亿元，同比增长11%。",
            is_ood=False,
            answer_cfg=ANSWER_CFG,
            judge_cfg=JUDGE_CFG,
        )
        assert res["answer"].startswith("营收")
        assert res["answer_metrics"]["correctness"] == 1.0
        assert res["answer_metrics"]["numeric_hit"] == 1.0
        assert res["hit"] is True  # 检索指标不受影响

    def test_generate_without_cfg_ignored(self):
        cfg = validate_config({"retrieval_mode": "dense", "rerank": False, "top_k": 3})
        res = run_config_for_question(
            query="q", gold_file_keys=["gold.md"], config=cfg, dense_embedding=[0.1],
            milvus=_FakeMilvus(), kb_scope="exp:d1", generate=True,
        )
        assert "answer_metrics" not in res