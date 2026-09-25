"""端到端问答评测单元测试：确定性指标、judge JSON 容错、聚合口径。"""

from __future__ import annotations

from app.experiment.answer_eval import parse_judge_json
from app.experiment.metrics import (
    aggregate_results,
    char_f1,
    exact_match,
    extract_numbers,
    normalize_answer,
    numeric_hit,
)


class TestNormalize:
    def test_strip_punct_and_space(self):
        assert normalize_answer(" 1492.08 亿元，同比增长 11%！") == "149208亿元同比增长11"

    def test_strip_citation_markers(self):
        assert normalize_answer("营收增长11%[1][12]") == "营收增长11"
        assert normalize_answer("营收增长11%【1】") == "营收增长11"

    def test_full_width_digits(self):
        assert normalize_answer("１２３") == "123"

    def test_empty(self):
        assert normalize_answer(None) == ""


class TestExactMatch:
    def test_equal_after_normalize(self):
        assert exact_match("营收 1492.08 亿元。", "营收1492.08亿元") == 1.0

    def test_different(self):
        assert exact_match("营收1492亿", "营收375.48亿") == 0.0

    def test_empty_ref(self):
        assert exact_match("任意答案", "") is None


class TestCharF1:
    def test_identical(self):
        assert char_f1("腾讯营收", "腾讯营收") == 1.0

    def test_disjoint(self):
        assert char_f1("abcd", "wxyz") == 0.0

    def test_partial(self):
        # 交集 2 字：pred 长 4、ref 长 4 → P=R=0.5 → F1=0.5
        assert abs(char_f1("abcd", "abxy") - 0.5) < 1e-9

    def test_empty(self):
        assert char_f1("", "abc") is None


class TestNumeric:
    def test_extract_ignores_thousand_separator(self):
        assert extract_numbers("1,492.08 亿") == [1492.08]

    def test_hit_with_tolerance(self):
        assert numeric_hit("营收1492.1亿元", "营收1492.08亿元") == 1.0
        # 相对容差 1% ≈ ±14.9，1600 明显超差
        assert numeric_hit("营收1600亿元", "营收1492.08亿元") == 0.0

    def test_multi_numbers_all_required(self):
        assert numeric_hit("同比增长11%，营收1492.08亿", "营收1492.08亿，同比增长11%") == 1.0
        assert numeric_hit("同比增长12%，营收1492.08亿", "营收1492.08亿，同比增长11%") == 0.0

    def test_ref_without_number_not_applicable(self):
        assert numeric_hit("任意", "没有数字的参考答案") is None

    def test_percent_symbol_ignored(self):
        assert numeric_hit("增长11%", "增长11%") == 1.0


class TestParseJudgeJson:
    def test_plain_json(self):
        data, err = parse_judge_json('{"score": 1, "reason": "ok"}')
        assert err is None and data["score"] == 1

    def test_fenced_json(self):
        data, err = parse_judge_json('```json\n{"refused": true}\n```')
        assert err is None and data["refused"] is True

    def test_with_surrounding_prose(self):
        data, err = parse_judge_json('评审结果如下：{"score": 0.5} 请参考。')
        assert err is None and data["score"] == 0.5

    def test_malformed(self):
        data, err = parse_judge_json("not a json at all")
        assert data is None and err == "judge_no_json"

    def test_empty(self):
        data, err = parse_judge_json("")
        assert data is None and err == "judge_empty_output"


class TestAggregateGeneration:
    def _rows(self):
        return [
            {
                "is_ood": False,
                "stratum": "numeric",
                "hit": True,
                "hit_rank": 1,
                "reciprocal_rank": 1.0,
                "recall": 1.0,
                "latency_ms": 10,
                "answer_latency_ms": 100,
                "answer_metrics": {
                    "em": 1.0,
                    "f1": 0.9,
                    "numeric_hit": 1.0,
                    "correctness": 1.0,
                    "faithfulness": 1.0,
                },
                "error": None,
            },
            {
                "is_ood": False,
                "stratum": "named",
                "hit": False,
                "hit_rank": 0,
                "reciprocal_rank": 0.0,
                "recall": 0.0,
                "latency_ms": 20,
                "answer_latency_ms": 200,
                "answer_metrics": {
                    "em": 0.0,
                    "f1": 0.2,
                    "numeric_hit": None,
                    "correctness": 0.0,
                    "faithfulness": 0.5,
                    "judge_error": "judge_bad_json",
                },
                "error": None,
            },
            {
                "is_ood": True,
                "hit": False,
                "hit_rank": 0,
                "reciprocal_rank": 0.0,
                "recall": 0.0,
                "latency_ms": 30,
                "answer_latency_ms": 300,
                "answer_metrics": {"refused": True},
                "error": None,
            },
            {
                "is_ood": True,
                "hit": False,
                "hit_rank": 0,
                "reciprocal_rank": 0.0,
                "recall": 0.0,
                "latency_ms": 40,
                "answer_latency_ms": 400,
                "answer_metrics": {"refused": False},
                "error": None,
            },
        ]

    def test_generation_metrics(self):
        agg = aggregate_results(self._rows())
        g = agg["generation"]
        assert g["count"] == 4
        assert g["correctness_mean"] == 0.5
        assert g["correct_rate"] == 0.5
        assert g["incorrect_rate"] == 0.5
        assert g["legacy_partial_count"] == 0
        assert g["pass_exact_rate"] == 0.5
        assert g["pass_partial_rate"] == 0.5
        assert g["faithfulness_mean"] == 0.75
        assert g["hallucination_rate"] == 0.25
        assert g["numeric"] == {"count": 1, "accuracy": 1.0}
        assert g["ood"]["count"] == 2 and g["ood"]["correct_refusal_rate"] == 0.5
        assert g["ood"]["false_answer_rate"] == 0.5
        assert g["avg_answer_latency_ms"] == 250
        assert g["judge_error_count"] == 1
        assert g["by_stratum"]["numeric"]["count"] == 1
        assert g["by_stratum"]["numeric"]["correct_rate"] == 1.0

    def test_legacy_partial_compat(self):
        def row(stratum, correctness):
            return {
                "is_ood": False,
                "stratum": stratum,
                "hit": True,
                "hit_rank": 1,
                "reciprocal_rank": 1.0,
                "recall": 1.0,
                "latency_ms": 1,
                "answer_latency_ms": 10,
                "answer_metrics": {"correctness": correctness, "em": 0.0, "f1": 0.5},
                "error": None,
            }

        g = aggregate_results(
            [row("numeric", 1.0), row("named", 0.5), row("multi", 0.0)]
        )["generation"]
        assert g["correctness_mean"] == 0.5
        assert g["correct_rate"] == 0.3333
        assert g["incorrect_rate"] == 0.6667
        assert g["legacy_partial_count"] == 1
        assert g["pass_partial_rate"] == 0.6667

    def test_retrieval_metrics_untouched(self):
        agg = aggregate_results(self._rows())
        assert agg["hit_rate"] == 0.5 and agg["mrr"] == 0.5

    def test_no_generation_when_no_answer_metrics(self):
        rows = [
            {"is_ood": False, "hit": True, "hit_rank": 1, "reciprocal_rank": 1.0, "recall": 1.0, "latency_ms": 1, "error": None},
        ]
        agg = aggregate_results(rows)
        assert "generation" not in agg

    def test_empty(self):
        assert "generation" not in aggregate_results([])