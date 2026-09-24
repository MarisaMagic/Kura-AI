"""实验平台服务层：数据集/问题集/运行的 CRUD、RAG_test 问题导入、级联清理。

实验知识库复用智能体 KB 全套管线（ingest/向量/元数据），仅 kb_scope 使用
专用前缀 exp:d{dataset_id}；对象存储前缀固定挂在 user_0 下（真实用户 ID 恒 > 0，无冲突）。
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.chat.database import SessionLocal
from app.chat.db_models import ExpDataset, ExpQuestion, ExpRun, ExpRunResult, KbDocument
from app.experiment.metrics import aggregate_results
from app.experiment.runner import validate_config
from app.kb import kb_service

# 实验文档在对象存储/流水线中的虚拟归属（user 0 不存在，前缀天然隔离）
EXP_KB_USER_ID = 0


def exp_kb_scope(dataset_id: int) -> str:
    """实验数据集专用 kb_scope。"""
    return f"exp:d{int(dataset_id)}"


def exp_agent_id(dataset_id: int) -> int:
    """实验数据集复用 KB 管线时的虚拟 agent_id（决定对象存储前缀 user_0/{id}）。"""
    return int(dataset_id)


# ---------------------------------------------------------------- datasets


def create_dataset(name: str, description: str, created_by: int) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("数据集名称不能为空")
    db = SessionLocal()
    try:
        ds = ExpDataset(name=name[:128], description=(description or "").strip(), created_by=int(created_by or 0))
        db.add(ds)
        db.commit()
        return _dataset_dict(ds)
    finally:
        db.close()


def list_datasets() -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.query(ExpDataset).order_by(ExpDataset.created_at.desc()).all()
        return [_dataset_dict(r) for r in rows]
    finally:
        db.close()


def get_dataset(dataset_id: int) -> dict | None:
    db = SessionLocal()
    try:
        ds = db.query(ExpDataset).filter(ExpDataset.id == int(dataset_id)).first()
        return _dataset_dict(ds) if ds else None
    finally:
        db.close()


def _dataset_dict(ds: ExpDataset) -> dict:
    return {
        "id": ds.id,
        "name": ds.name,
        "description": ds.description,
        "doc_count": ds.doc_count or 0,
        "question_count": ds.question_count or 0,
        "created_by": ds.created_by,
        "created_at": ds.created_at.isoformat() if ds.created_at else None,
        "updated_at": ds.updated_at.isoformat() if ds.updated_at else None,
    }


def refresh_dataset_counts(dataset_id: int) -> None:
    """重算数据集的文档/问题计数缓存（低频调用：详情页、问题集变更）。

    批量上传过程中「每个文档完成」不再走这里（全表 COUNT 会拖慢事件循环），
    改由 refresh_document_count 以增量方式更新，见 kb_job._on_task_finished。
    """
    scope = exp_kb_scope(dataset_id)
    db = SessionLocal()
    try:
        doc_count = db.query(KbDocument).filter(KbDocument.kb_scope == scope).count()
        q_count = db.query(ExpQuestion).filter(ExpQuestion.dataset_id == int(dataset_id)).count()
        ds = db.query(ExpDataset).filter(ExpDataset.id == int(dataset_id)).first()
        if ds:
            ds.doc_count = doc_count
            ds.question_count = q_count
            db.commit()
    finally:
        db.close()


def refresh_document_count(dataset_id: int) -> None:
    """重算单个数据集的文档数（上传任务终态在 worker 线程内调用）。

    仅在上传成功（新文档入库）或删除文档时调用，避免每完成一个文档都对
    mg_kb_documents 做全表 COUNT 而阻塞事件循环。
    """
    scope = exp_kb_scope(dataset_id)
    db = SessionLocal()
    try:
        doc_count = db.query(KbDocument).filter(KbDocument.kb_scope == scope).count()
        db.query(ExpDataset).filter(ExpDataset.id == int(dataset_id)).update(
            {ExpDataset.doc_count: doc_count}, synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def delete_dataset(dataset_id: int) -> bool:
    """级联删除数据集：Milvus 向量、KB 元数据/父块/图片/对象存储、问题、运行及结果。"""
    dataset_id = int(dataset_id)
    scope = exp_kb_scope(dataset_id)
    # 复用 KB 清理（Milvus + 父块 + mg_kb_documents + 图片 + MinIO 前缀 user_0/{dataset_id}）
    kb_service.purge_kb_for_scope(scope, EXP_KB_USER_ID, exp_agent_id(dataset_id))
    db = SessionLocal()
    try:
        run_ids = [r[0] for r in db.query(ExpRun.id).filter(ExpRun.dataset_id == dataset_id).all()]
        if run_ids:
            db.query(ExpRunResult).filter(ExpRunResult.run_id.in_(run_ids)).delete(synchronize_session=False)
            db.query(ExpRun).filter(ExpRun.id.in_(run_ids)).delete(synchronize_session=False)
        db.query(ExpQuestion).filter(ExpQuestion.dataset_id == dataset_id).delete(synchronize_session=False)
        db.query(ExpDataset).filter(ExpDataset.id == dataset_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return True


# ---------------------------------------------------------------- documents


def list_documents(dataset_id: int) -> list[dict]:
    return kb_service.fetch_kb_document_list(exp_kb_scope(dataset_id))


def delete_document(dataset_id: int, display_filename: str) -> bool:
    ok = kb_service.delete_kb_document(
        exp_kb_scope(dataset_id), EXP_KB_USER_ID, exp_agent_id(dataset_id), display_filename
    )
    refresh_dataset_counts(dataset_id)
    return ok


# ---------------------------------------------------------------- questions


def parse_question_file(raw: bytes) -> list[dict]:
    """
    解析问题集文件（JSON / JSONL），兼容 RAG_test 的 dataset.json 与 ood_questions.json。
    gold 来源优先级：file_keys > file_key > documents > document（路径取 basename）。
    document_in_pack == false 或 gold 为空 → 记为 OOD 题。
    :return: [{ext_id, question, answer, gold_file_keys, stratum, is_ood}]
    """
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise ValueError("问题集文件为空")
    cases: list[Any] = []
    data = None
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None  # 形如 JSONL（首行以 { 开头），转按行解析
    if data is not None:
        if isinstance(data, list):
            cases = data
        elif isinstance(data, dict):
            cases = data.get("cases") or data.get("questions") or data.get("data") or []
    else:
        for line in text.splitlines():
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    if not isinstance(cases, list) or not cases:
        raise ValueError("未解析到任何问题条目（支持 {cases: [...]} / [...] / JSONL）")

    parsed: list[dict] = []
    for c in cases:
        if not isinstance(c, dict):
            continue
        question = str(c.get("question") or c.get("q") or "").strip()
        if not question:
            continue
        gold: list[str] = []
        for key in ("file_keys", "gold_file_keys"):
            v = c.get(key)
            if isinstance(v, list) and v:
                gold = [os.path.basename(str(x)).strip() for x in v if str(x).strip()]
                break
        if not gold:
            v = c.get("file_key")
            if isinstance(v, str) and v.strip():
                gold = [os.path.basename(v.strip())]
        if not gold:
            for key in ("documents", "document", "docs"):
                v = c.get(key)
                items = v if isinstance(v, list) else ([v] if isinstance(v, str) else [])
                names = [os.path.basename(str(x)).strip() for x in items if str(x).strip()]
                if names:
                    gold = names
                    break
        is_ood = bool(c.get("is_ood")) or c.get("document_in_pack") is False or not gold
        parsed.append(
            {
                "ext_id": str(c.get("id") or "")[:64],
                "question": question,
                "answer": str(c.get("answer") or c.get("a") or "").strip(),
                "gold_file_keys": [] if is_ood else gold,
                "stratum": str(c.get("stratum") or "")[:32],
                "is_ood": is_ood,
            }
        )
    if not parsed:
        raise ValueError("文件中没有包含 question 字段的有效条目")
    return parsed


def dedupe_parsed_questions(parsed: list[dict], existing_texts: set[str]) -> tuple[list[dict], int]:
    """
    按问题文本去重（与已有问题集及本批次内部均比对）。
    :param parsed: parse_question_file 输出条目
    :param existing_texts: 数据集中已存在问题文本集合
    :return: (保留条目, 跳过条数)
    """
    seen = {(t or "").strip() for t in existing_texts}
    kept: list[dict] = []
    for p in parsed:
        key = p["question"].strip()
        if key in seen:
            continue
        seen.add(key)
        kept.append(p)
    return kept, len(parsed) - len(kept)


def import_questions(dataset_id: int, raw: bytes, *, replace: bool = False) -> dict:
    """导入问题集；replace=True 时先清空同数据集旧问题。

    同数据集内按问题文本去重（含本批次内部重复），避免重复导入同一文件。
    """
    parsed = parse_question_file(raw)
    db = SessionLocal()
    try:
        if replace:
            db.query(ExpQuestion).filter(ExpQuestion.dataset_id == int(dataset_id)).delete(synchronize_session=False)
            existing: set[str] = set()
        else:
            existing = {
                (r[0] or "").strip()
                for r in db.query(ExpQuestion.question).filter(ExpQuestion.dataset_id == int(dataset_id)).all()
            }
        kept, skipped = dedupe_parsed_questions(parsed, existing)
        for p in kept:
            db.add(
                ExpQuestion(
                    dataset_id=int(dataset_id),
                    ext_id=p["ext_id"],
                    question=p["question"],
                    answer=p["answer"],
                    gold_file_keys=p["gold_file_keys"],
                    stratum=p["stratum"],
                    is_ood=p["is_ood"],
                )
            )
        db.commit()
    finally:
        db.close()
    refresh_dataset_counts(dataset_id)
    return {
        "imported": len(kept),
        "skipped": skipped,
        "ood_imported": sum(1 for p in kept if p["is_ood"]),
        "replaced": bool(replace),
    }


def list_questions(dataset_id: int, page: int = 1, page_size: int = 20, is_ood: bool | None = None) -> dict:
    db = SessionLocal()
    try:
        q = db.query(ExpQuestion).filter(ExpQuestion.dataset_id == int(dataset_id))
        if is_ood is not None:
            q = q.filter(ExpQuestion.is_ood == is_ood)
        total = q.count()
        rows = q.order_by(ExpQuestion.id.asc()).offset(max(0, (page - 1) * page_size)).limit(page_size).all()
        base = max(0, (page - 1) * page_size)
        items = [
            {
                "index": base + i,
                "id": r.id,
                "ext_id": r.ext_id,
                "question": r.question,
                "answer": r.answer,
                "gold_file_keys": r.gold_file_keys or [],
                "stratum": r.stratum,
                "is_ood": bool(r.is_ood),
            }
            for i, r in enumerate(rows, 1)
        ]
        return {"total": total, "items": items}
    finally:
        db.close()


def unmatched_gold_keys(dataset_id: int) -> list[str]:
    """问题集 gold 文件名中未在已上传文档里出现的部分（导入后校验提示用）。"""
    scope = exp_kb_scope(dataset_id)
    db = SessionLocal()
    try:
        doc_names = {
            r[0] for r in db.query(KbDocument.display_filename).filter(KbDocument.kb_scope == scope).all()
        }
        golds: set[str] = set()
        for (keys,) in db.query(ExpQuestion.gold_file_keys).filter(
            ExpQuestion.dataset_id == int(dataset_id), ExpQuestion.is_ood == False  # noqa: E712
        ).all():
            golds.update(str(k) for k in (keys or []))
        return sorted(golds - doc_names)
    finally:
        db.close()


def delete_question(question_id: int) -> bool:
    db = SessionLocal()
    try:
        row = db.query(ExpQuestion).filter(ExpQuestion.id == int(question_id)).first()
        if not row:
            return False
        dataset_id = row.dataset_id
        db.delete(row)
        db.commit()
    finally:
        db.close()
    refresh_dataset_counts(dataset_id)
    return True


def clear_questions(dataset_id: int, ood_only: bool = False) -> int:
    db = SessionLocal()
    try:
        q = db.query(ExpQuestion).filter(ExpQuestion.dataset_id == int(dataset_id))
        if ood_only:
            q = q.filter(ExpQuestion.is_ood == True)  # noqa: E712
        n = q.delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    refresh_dataset_counts(dataset_id)
    return int(n)


# ---------------------------------------------------------------- runs


def create_run(
    dataset_id: int,
    name: str,
    configs: list[dict],
    question_limit: int,
    include_ood: bool,
    created_by: int,
) -> dict:
    """创建运行记录（status=queued），配置逐个校验规范化。"""
    dataset_id = int(dataset_id)
    if not configs:
        raise ValueError("至少需要一个实验配置")
    if len(configs) > 12:
        raise ValueError("单次运行最多 12 个配置")
    norm = [validate_config(c) for c in configs]
    db = SessionLocal()
    try:
        ds = db.query(ExpDataset).filter(ExpDataset.id == dataset_id).first()
        if not ds:
            raise ValueError("数据集不存在")
        if not (ds.doc_count or 0):
            raise ValueError("数据集尚未上传文档，无法运行实验")
        q = db.query(ExpQuestion).filter(ExpQuestion.dataset_id == dataset_id, ExpQuestion.is_ood == False)  # noqa: E712
        if q.count() == 0:
            raise ValueError("数据集尚未导入问题（库内题），无法运行实验")
        run = ExpRun(
            dataset_id=dataset_id,
            name=(name or "").strip()[:128] or f"运行 #{ds.id}",
            configs=norm,
            question_limit=max(0, int(question_limit or 0)),
            include_ood=bool(include_ood),
            snapshot={"doc_count": ds.doc_count or 0, "question_count": ds.question_count or 0},
            status="queued",
            created_by=int(created_by or 0),
        )
        db.add(run)
        db.commit()
        return _run_dict(run)
    finally:
        db.close()


def list_runs(dataset_id: int | None = None) -> list[dict]:
    db = SessionLocal()
    try:
        q = db.query(ExpRun)
        if dataset_id is not None:
            q = q.filter(ExpRun.dataset_id == int(dataset_id))
        rows = q.order_by(ExpRun.created_at.desc()).all()
        return [_run_dict(r) for r in rows]
    finally:
        db.close()


def get_run(run_id: int) -> dict | None:
    db = SessionLocal()
    try:
        r = db.query(ExpRun).filter(ExpRun.id == int(run_id)).first()
        return _run_dict(r) if r else None
    finally:
        db.close()


def _run_dict(r: ExpRun) -> dict:
    return {
        "id": r.id,
        "dataset_id": r.dataset_id,
        "name": r.name,
        "configs": r.configs or [],
        "question_limit": r.question_limit or 0,
        "include_ood": bool(r.include_ood),
        "snapshot": r.snapshot or {},
        "status": r.status,
        "created_by": r.created_by,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


def delete_run(run_id: int) -> bool:
    db = SessionLocal()
    try:
        db.query(ExpRunResult).filter(ExpRunResult.run_id == int(run_id)).delete(synchronize_session=False)
        n = db.query(ExpRun).filter(ExpRun.id == int(run_id)).delete(synchronize_session=False)
        db.commit()
        return bool(n)
    finally:
        db.close()


def get_run_results(run_id: int) -> dict | None:
    """运行结果：每配置聚合指标 + 逐题明细（含 retrieved 文档列表）。"""
    run = get_run(run_id)
    if not run:
        return None
    db = SessionLocal()
    try:
        rows = db.query(ExpRunResult).filter(ExpRunResult.run_id == int(run_id)).all()
        qids = {r.question_id for r in rows}
        qmap: dict[int, dict] = {}
        if qids:
            # 数据集内序号（按 id 升序，从 1 起）：前端展示与问题集列表一致
            order_index = {
                row[0]: i
                for i, row in enumerate(
                    db.query(ExpQuestion.id)
                    .filter(ExpQuestion.dataset_id == run["dataset_id"])
                    .order_by(ExpQuestion.id.asc())
                    .all(),
                    1,
                )
            }
            for q in db.query(ExpQuestion).filter(ExpQuestion.id.in_(qids)).all():
                qmap[q.id] = {
                    "index": order_index.get(q.id, 0),
                    "id": q.id,
                    "question": q.question,
                    "answer": q.answer,
                    "gold_file_keys": q.gold_file_keys or [],
                    "stratum": q.stratum,
                    "is_ood": bool(q.is_ood),
                }
    finally:
        db.close()

    configs = run["configs"] or []
    by_cfg: dict[int, list[dict]] = {i: [] for i in range(len(configs))}
    details: dict[int, dict] = {}
    for r in rows:
        row = {
            "hit": bool(r.hit),
            "hit_rank": r.hit_rank,
            "reciprocal_rank": float(r.reciprocal_rank or 0.0),
            "recall": float(r.recall or 0.0),
            "is_ood": bool(r.is_ood),
            "top1_score": float(r.top1_score or 0.0),
            "max_rerank_score": r.max_rerank_score,
            "rerank_below_min": bool(r.rerank_below_min),
            "latency_ms": r.latency_ms,
            "error": r.error,
        }
        by_cfg.setdefault(r.config_idx, []).append(row)
        d = details.setdefault(
            r.question_id,
            {"question": qmap.get(r.question_id, {}), "by_config": {}},
        )
        d["by_config"][str(r.config_idx)] = {**row, "retrieved": r.retrieved or []}

    summary = []
    for i, cfg in enumerate(configs):
        summary.append({"config_idx": i, "config": cfg, "metrics": aggregate_results(by_cfg.get(i, []))})
    per_question = sorted(details.values(), key=lambda d: (d["question"].get("is_ood", False), d["question"].get("id", 0)))
    return {"run": run, "summary": summary, "per_question": per_question}
