"""rag_pipeline 打分 / 路由 / 拒答分支测试（LLM 全桩、零网络）。

覆盖三类节点的路由决策：
- grade_documents_node：空检索 / 无模型 / 打分通过 / 全不相关 / 打分异常 → 各自路由；
- grade_expanded_node：拒答开关 / 空结果 / 无模型 / 二次通过 / 二次全不相关 → generate_answer 或 no_answer；
- rewrite_question_node：strategy 选择、complex 默认降级、无路由模型兜底。
"""

import unittest
from unittest import mock

from pydantic import BaseModel

from app.kb import rag_pipeline
from app.kb.rag_pipeline import ChunkGrades, ChunkRating, RAGState, RewriteStrategy


def _docs(n: int) -> list[dict]:
    return [{"filename": f"doc{i}.md", "page_number": i, "text": f"第 {i} 段内容"} for i in range(1, n + 1)]


def _grades(*yes_indices: int) -> ChunkGrades:
    ratings = []
    for i, _doc in enumerate(_docs(max(yes_indices, default=1)), 1):
        score = "yes" if i in set(yes_indices) else "no"
        ratings.append(ChunkRating(chunk_index=i, binary_score=score))
    return ChunkGrades(ratings=ratings)


class _FakeModel:
    """with_structured_output(...).invoke(...) 返回预设 pydantic 对象的桩模型。"""

    def __init__(self, result: BaseModel | None = None):
        self._result = result
        self.last_messages = None

    def with_structured_output(self, schema, method="json_mode"):
        self._schema = schema
        return self

    def invoke(self, messages):
        self.last_messages = messages
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeGradingModel(_FakeModel):
    """按 schema 返回 ChunkGrades 的评分桩。"""

    def with_structured_output(self, schema, method="json_mode"):
        super().with_structured_output(schema, method)
        assert schema is ChunkGrades
        return self


def _base_state(**overrides) -> RAGState:
    state: RAGState = {
        "question": "什么是知识库检索",
        "kb_scope": "kb:scope:1",
        "llm_config": {"api_key": "fake-key", "model_name": "gpt-4", "base_url": "http://fake"},
        "docs": _docs(2),
        "rag_trace": {},
        "route": None,
        "expansion_type": None,
        "expanded_query": None,
        "step_back_question": None,
        "step_back_answer": None,
        "hypothetical_doc": None,
        "query": "",
        "context": "",
    }
    state.update(overrides)
    return state


class GradeDocumentsNodeTests(unittest.TestCase):
    """首次检索打分：任一相关 → generate_answer；全不相关 / 无结果 / 无模型 → rewrite_question。"""

    def test_empty_docs_routes_to_rewrite(self) -> None:
        # 节点在空结果分支前也会先构造 grader，须一并打桩避免真实建链（DNS）
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=None):
            out = rag_pipeline.grade_documents_node(_base_state(docs=[]))
        self.assertEqual(out["route"], "rewrite_question")
        self.assertEqual(out["rag_trace"]["grade_score"], "no")
        self.assertTrue(out["rag_trace"]["rewrite_needed"])

    def test_no_grader_model_routes_to_rewrite(self) -> None:
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=None):
            out = rag_pipeline.grade_documents_node(_base_state())
        self.assertEqual(out["route"], "rewrite_question")
        self.assertEqual(out["rag_trace"]["grade_score"], "unknown")

    def test_any_yes_routes_to_generate(self) -> None:
        model = _FakeGradingModel(_grades(1))
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=model):
            out = rag_pipeline.grade_documents_node(_base_state())
        self.assertEqual(out["route"], "generate_answer")
        self.assertEqual(out["rag_trace"]["grade_score"], "yes")
        self.assertFalse(out["rag_trace"]["rewrite_needed"])
        self.assertEqual(out["rag_trace"]["per_chunk_grades"][0]["binary_score"], "yes")

    def test_all_no_routes_to_rewrite(self) -> None:
        model = _FakeGradingModel(_grades())
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=model):
            out = rag_pipeline.grade_documents_node(_base_state())
        self.assertEqual(out["route"], "rewrite_question")
        self.assertTrue(out["rag_trace"]["rewrite_needed"])

    def test_missing_ratings_default_no_for_uncovered_chunk(self) -> None:
        # 只对第 2 块给出 yes，第 1 块缺失 → 按 no 处理，仍有任一相关放行生成
        model = _FakeGradingModel(ChunkGrades(ratings=[ChunkRating(chunk_index=2, binary_score="yes")]))
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=model):
            out = rag_pipeline.grade_documents_node(_base_state())
        grades = out["rag_trace"]["per_chunk_grades"]
        self.assertEqual(grades[0]["binary_score"], "no")
        self.assertEqual(grades[1]["binary_score"], "yes")
        self.assertEqual(out["route"], "generate_answer")

    def test_grader_exception_falls_through_to_generate(self) -> None:
        model = _FakeGradingModel(RuntimeError("上游接口 5xx"))
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=model):
            out = rag_pipeline.grade_documents_node(_base_state())
        self.assertEqual(out["route"], "generate_answer")
        self.assertEqual(out["rag_trace"]["grade_score"], "unknown")
        self.assertIn("grade_error", out["rag_trace"])


class GradeExpandedNodeTests(unittest.TestCase):
    """扩展检索二次门控：全不相关 / 空结果且拒答开启 → no_answer（由工具侧转拒答文案）。"""

    def test_refusal_disabled_skips_second_grade(self) -> None:
        with mock.patch.object(rag_pipeline.settings, "KB_GRADE_REFUSAL_ENABLED", False):
            out = rag_pipeline.grade_expanded_node(_base_state())
        self.assertEqual(out["route"], "generate_answer")
        self.assertEqual(out["rag_trace"]["second_grade"], "skipped")

    def test_empty_docs_no_answer(self) -> None:
        out = rag_pipeline.grade_expanded_node(_base_state(docs=[]))
        self.assertEqual(out["route"], "no_answer")
        self.assertTrue(out.get("no_answer"))

    def test_no_grader_falls_through_to_generate(self) -> None:
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=None):
            out = rag_pipeline.grade_expanded_node(_base_state())
        self.assertEqual(out["route"], "generate_answer")
        self.assertEqual(out["rag_trace"]["second_grade"], "no_model")

    def test_second_grade_pass_generates(self) -> None:
        model = _FakeGradingModel(_grades(1))
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=model):
            out = rag_pipeline.grade_expanded_node(_base_state())
        self.assertEqual(out["route"], "generate_answer")
        self.assertEqual(out["rag_trace"]["second_grade"], "pass")

    def test_second_grade_all_no_answers_no_answer(self) -> None:
        model = _FakeGradingModel(_grades())
        with mock.patch.object(rag_pipeline, "_grader_model", return_value=model):
            out = rag_pipeline.grade_expanded_node(_base_state())
        self.assertEqual(out["route"], "no_answer")
        self.assertTrue(out.get("no_answer"))
        self.assertEqual(out["rag_trace"]["second_grade"], "fail_all")


class RewriteQuestionNodeTests(unittest.TestCase):
    """查询重写策略选择：router 输出映射到 step_back/hyde/complex，complex 默认降级。"""

    def setUp(self) -> None:
        self.step_back_patcher = mock.patch.object(
            rag_pipeline,
            "step_back_expand",
            return_value={"step_back_question": "退步问题", "step_back_answer": "退步回答", "expanded_query": "扩展查询"},
        )
        self.hyde_patcher = mock.patch.object(rag_pipeline, "generate_hypothetical_document", return_value="假设文档")
        self.step_back = self.step_back_patcher.start()
        self.hyde = self.hyde_patcher.start()

    def tearDown(self) -> None:
        self.step_back_patcher.stop()
        self.hyde_patcher.stop()

    def _run_with_router(self, result: RewriteStrategy | BaseModel | None, *, with_model: bool = True) -> dict:
        model = _FakeModel(result) if with_model else None
        with mock.patch.object(rag_pipeline, "_router_model", return_value=model):
            return rag_pipeline.rewrite_question_node(_base_state())

    def test_step_back_strategy(self) -> None:
        out = self._run_with_router(RewriteStrategy(strategy="step_back"))
        self.assertEqual(out["expansion_type"], "step_back")
        self.assertEqual(out["step_back_question"], "退步问题")
        self.assertEqual(self.step_back.call_count, 1)
        self.assertEqual(self.hyde.call_count, 0)

    def test_hyde_strategy(self) -> None:
        out = self._run_with_router(RewriteStrategy(strategy="hyde"))
        self.assertEqual(out["expansion_type"], "hyde")
        self.assertEqual(out["hypothetical_doc"], "假设文档")
        self.assertEqual(self.hyde.call_count, 1)
        self.assertEqual(self.step_back.call_count, 0)

    def test_complex_downgraded_to_step_back_when_disabled(self) -> None:
        # RAG_ALLOW_COMPLEX_STRATEGY 默认 False（config.py:226）
        out = self._run_with_router(RewriteStrategy(strategy="complex"))
        self.assertEqual(out["expansion_type"], "step_back")
        self.assertEqual(out["rag_trace"]["rewrite_strategy"], "step_back")
        self.assertEqual(self.step_back.call_count, 1)
        self.assertEqual(self.hyde.call_count, 0)

    def test_no_router_defaults_to_step_back(self) -> None:
        out = self._run_with_router(None, with_model=False)
        self.assertEqual(out["expansion_type"], "step_back")

    def test_router_exception_defaults_to_step_back(self) -> None:
        out = self._run_with_router(RuntimeError("路由接口异常"))
        self.assertEqual(out["expansion_type"], "step_back")
        self.assertEqual(self.step_back.call_count, 1)


if __name__ == "__main__":
    unittest.main()
