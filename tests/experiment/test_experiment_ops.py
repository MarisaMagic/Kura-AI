"""实验平台运维纯逻辑测试：时区序列化、自动命名、进度单元、请求模型校验。"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.experiment.exp_job import planned_units
from app.experiment.service import _auto_run_name, _local_iso, _local_iso_str
from app.schemas.experiment import ExpDocsDelete, ExpRunCreate


class TestLocalIso:
    def test_utc_to_beijing(self):
        assert _local_iso(datetime(2026, 9, 24, 5, 30, 0)) == "2026-09-24T13:30:00+08:00"

    def test_aware_passthrough_convert(self):
        from datetime import timezone

        dt = datetime(2026, 9, 24, 5, 30, 0, tzinfo=timezone.utc)
        assert _local_iso(dt) == "2026-09-24T13:30:00+08:00"

    def test_none(self):
        assert _local_iso(None) is None

    def test_iso_str(self):
        assert _local_iso_str("2026-09-24T05:30:00") == "2026-09-24T13:30:00+08:00"

    def test_iso_str_invalid_kept(self):
        assert _local_iso_str("not-a-time") == "not-a-time"


class TestAutoRunName:
    def test_first(self):
        assert _auto_run_name("RAG_test-1doc", "retrieval", []) == "RAG_test-1doc · 检索消融 #1"

    def test_seq_increments_from_max(self):
        names = ["RAG_test-1doc · 检索消融 #1", "RAG_test-1doc · 检索消融 #3"]
        assert _auto_run_name("RAG_test-1doc", "retrieval", names) == "RAG_test-1doc · 检索消融 #4"

    def test_kind_isolated(self):
        names = ["RAG_test-1doc · 检索消融 #2"]
        assert _auto_run_name("RAG_test-1doc", "qa", names) == "RAG_test-1doc · 问答测评 #1"

    def test_other_dataset_not_counted(self):
        names = ["other · 问答测评 #5"]
        assert _auto_run_name("ds", "qa", names) == "ds · 问答测评 #1"

    def test_manual_name_collision_skipped(self):
        names = ["ds · 问答测评 #1", "ds · 问答测评 #2", "ds · 问答测评 #3"]
        assert _auto_run_name("ds", "qa", names) == "ds · 问答测评 #4"


class TestPlannedUnits:
    def test_retrieval(self):
        questions = [{"is_ood": False}, {"is_ood": False}]
        assert planned_units(questions, 3, "retrieval", True) == 1 + 1 + 2 * 3

    def test_qa_mixed_ood(self):
        questions = [{"is_ood": False}, {"is_ood": True}]
        assert planned_units(questions, 1, "qa", True) == 1 + 1 + (1 + 3) + (1 + 2)

    def test_sparse_only(self):
        assert planned_units([{"is_ood": False}], 1, "retrieval", False) == 1 + 1

    def test_never_zero(self):
        assert planned_units([], 0, "retrieval", False) == 1


class TestSchemas:
    def test_run_kind_literal(self):
        with pytest.raises(ValidationError):
            ExpRunCreate(dataset_id=1, kind="foo", configs=[{"retrieval_mode": "dense"}])

    def test_run_kind_defaults_retrieval(self):
        body = ExpRunCreate(dataset_id=1, configs=[{"retrieval_mode": "dense"}])
        assert body.kind == "retrieval"

    def test_docs_delete_requires_filenames(self):
        with pytest.raises(ValidationError):
            ExpDocsDelete(dataset_id=1, filenames=[])