"""实验平台 API（仅超级管理员，路由级 DependSuperuser）：数据集/文档/问题集/消融运行。"""

from __future__ import annotations

import asyncio
from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.core.dependency import SuperuserControl
from app.experiment import exp_job, service
from app.experiment.service import EXP_KB_USER_ID, exp_agent_id, exp_kb_scope
from app.kb import kb_job, kb_service
from app.models import User
from app.schemas.base import Fail, Success
from app.schemas.experiment import ExpDatasetCreate, ExpDocsDelete, ExpRunCreate
from app.settings import settings
from app.utils.upload_accept import prepare_document_upload

router = APIRouter(dependencies=[Depends(SuperuserControl.is_superuser)])

TAG = "实验模块"


# ---------------------------------------------------------------- datasets


@router.get("/datasets", summary="实验数据集列表", tags=[TAG])
async def exp_list_datasets(current_user: User = Depends(SuperuserControl.is_superuser)):
    def _load() -> list[dict]:
        for ds in service.list_datasets():
            service.refresh_dataset_counts(ds["id"])
        return service.list_datasets()

    return Success(data=await asyncio.to_thread(_load))


@router.post("/datasets", summary="创建实验数据集", tags=[TAG])
async def exp_create_dataset(
    body: ExpDatasetCreate,
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    try:
        ds = service.create_dataset(body.name, body.description, current_user.id)
    except ValueError as e:
        return Fail(code=400, msg=str(e))
    return Success(data=ds, msg="创建成功")


@router.get("/datasets/detail", summary="实验数据集详情（含 gold 未匹配提示）", tags=[TAG])
async def exp_dataset_detail(
    dataset_id: int = Query(..., description="数据集 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    def _load() -> dict | None:
        current = service.get_dataset(dataset_id)
        if not current:
            return None
        service.refresh_dataset_counts(dataset_id)
        current = service.get_dataset(dataset_id) or current
        current["unmatched_gold_keys"] = service.unmatched_gold_keys(dataset_id)
        current["answer_count"] = service.count_answered_questions(dataset_id)
        return current

    ds = await asyncio.to_thread(_load)
    if not ds:
        return Fail(code=404, msg="数据集不存在")
    return Success(data=ds)


@router.delete("/datasets", summary="删除实验数据集（级联清理向量/文档/问题/运行）", tags=[TAG])
async def exp_delete_dataset(
    dataset_id: int = Query(..., description="数据集 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    if not service.get_dataset(dataset_id):
        return Fail(code=404, msg="数据集不存在")
    try:
        service.delete_dataset(dataset_id)
    except Exception as e:  # noqa: BLE001
        return Fail(code=500, msg=str(e))
    return Success(data={"dataset_id": dataset_id}, msg="删除成功")


# ---------------------------------------------------------------- documents


@router.get("/documents", summary="实验数据集文档列表", tags=[TAG])
async def exp_list_documents(
    dataset_id: int = Query(..., description="数据集 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    if not service.get_dataset(dataset_id):
        return Fail(code=404, msg="数据集不存在")
    docs = await asyncio.to_thread(service.list_documents, dataset_id)
    return Success(data={"documents": docs})


@router.post("/documents/upload", summary="上传实验文档（后台处理，返回 task_id）", tags=[TAG])
async def exp_upload_document(
    dataset_id: int = Query(..., description="数据集 ID"),
    file: UploadFile = File(...),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    if not service.get_dataset(dataset_id):
        return Fail(code=404, msg="数据集不存在")
    display = kb_service.normalize_display_filename(file.filename or "")
    if not kb_service.allowed_upload_extension(display):
        return Fail(code=400, msg="仅支持 PDF、Word、Excel、TXT、Markdown 文档")
    if not (settings.EMBEDDING_API_KEY or "").strip():
        return Fail(code=400, msg="未配置 EMBEDDING_API_KEY，无法生成向量")
    task_id, error = await prepare_document_upload(
        file=file,
        display_filename=display,
        user_id=EXP_KB_USER_ID,
        agent_id=exp_agent_id(dataset_id),
        kb_scope=exp_kb_scope(dataset_id),
    )
    if error:
        code, msg = error
        return Fail(code=code, msg=msg)
    return Success(data={"task_id": task_id}, msg="上传已受理，正在后台处理")


@router.post("/documents/batch-delete", summary="批量删除实验文档（按当前筛选结果）", tags=[TAG])
async def exp_batch_delete_documents(
    body: ExpDocsDelete,
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    if not service.get_dataset(body.dataset_id):
        return Fail(code=404, msg="数据集不存在")
    try:
        result = await asyncio.to_thread(service.delete_documents, body.dataset_id, body.filenames)
    except ValueError as e:
        return Fail(code=400, msg=str(e))
    except Exception as e:  # noqa: BLE001
        return Fail(code=500, msg=str(e))
    if result["failed"]:
        return Success(
            data=result,
            msg=f"已删除 {result['deleted']} 个文档，{len(result['failed'])} 个删除失败",
        )
    return Success(data=result, msg=f"已删除 {result['deleted']} 个文档")


def _exp_task_meta(task_id: str) -> dict | None:
    """读取上传任务快照并校验属于实验数据集（kb_scope 前缀 exp:）。"""
    meta = kb_job.get_kb_upload_job_meta(task_id)
    if not meta or not str(meta.get("kb_scope") or "").startswith("exp:"):
        return None
    return meta


@router.get("/documents/upload/status", summary="查询实验文档上传任务进度", tags=[TAG])
async def exp_upload_status(
    task_id: str = Query(..., description="上传任务 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    # Redis 为同步客户端，统一放线程执行，避免大批量轮询阻塞事件循环
    meta = await asyncio.to_thread(_exp_task_meta, task_id)
    if not meta:
        return Fail(code=404, msg="上传任务不存在或已过期")
    # 数据集计数已由上传 worker 在任务终态时增量刷新（kb_job._on_task_finished），此处不再 COUNT
    return Success(data=meta)


@router.get("/documents/upload/status/batch", summary="批量查询实验文档上传任务进度", tags=[TAG])
async def exp_upload_status_batch(
    task_ids: str = Query(..., description="上传任务 ID，逗号分隔（<=200 个）"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    """一次返回多个任务快照，替代逐任务轮询（1000 个文档时把 2000QPS 降到个位数）。"""
    ids = [t.strip() for t in str(task_ids).split(",") if t.strip()][:200]
    if not ids:
        return Fail(code=400, msg="task_ids 不能为空")

    def _read_all() -> dict[str, dict]:
        # 一次 MGET 读回全部快照（逐任务 GET 在 200 并发轮询下会打满事件循环）
        metas = kb_job.get_kb_upload_job_meta_many(ids)
        return {
            tid: meta
            for tid, meta in metas.items()
            if str(meta.get("kb_scope") or "").startswith("exp:")
        }

    return Success(data={"items": await asyncio.to_thread(_read_all)})


@router.post("/documents/upload/cancel", summary="取消实验文档上传任务", tags=[TAG])
async def exp_upload_cancel(
    task_id: str = Query(..., description="上传任务 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    meta = await asyncio.to_thread(_exp_task_meta, task_id)
    if not meta:
        return Fail(code=404, msg="上传任务不存在或已过期")
    if meta.get("status") not in ("queued", "running"):
        return Fail(code=400, msg="任务已结束，无法取消")
    await kb_job.request_kb_upload_cancel(task_id)
    return Success(data={"task_id": task_id}, msg="已请求取消处理")


@router.delete("/documents", summary="删除实验数据集中的单个文档", tags=[TAG])
async def exp_delete_document(
    dataset_id: int = Query(..., description="数据集 ID"),
    filename: str = Query(..., description="展示文件名（原始上传名）"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    display = kb_service.normalize_display_filename(unquote(filename))
    try:
        # Milvus/对象存储/PG 均为同步阻塞调用，放线程执行避免卡住事件循环
        await asyncio.to_thread(service.delete_document, dataset_id, display)
    except Exception as e:  # noqa: BLE001
        return Fail(code=500, msg=str(e))
    return Success(data={"display_filename": display}, msg="删除成功")


# ---------------------------------------------------------------- questions


@router.post("/questions/import", summary="导入实验问题集（JSON/JSONL，兼容 RAG_test）", tags=[TAG])
async def exp_import_questions(
    dataset_id: int = Query(..., description="数据集 ID"),
    replace: bool = Query(False, description="导入前清空旧问题"),
    file: UploadFile = File(...),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    if not service.get_dataset(dataset_id):
        return Fail(code=404, msg="数据集不存在")
    raw = await file.read()
    try:
        result = service.import_questions(dataset_id, raw, replace=replace)
    except (ValueError, Exception) as e:  # noqa: BLE001
        return Fail(code=400, msg=f"问题集解析失败: {e}")
    result["unmatched_gold_keys"] = service.unmatched_gold_keys(dataset_id)
    return Success(data=result, msg=f"成功导入 {result['imported']} 个问题")


@router.get("/questions", summary="实验问题集分页列表", tags=[TAG])
async def exp_list_questions(
    dataset_id: int = Query(..., description="数据集 ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    is_ood: bool | None = Query(None, description="按 OOD 标记过滤"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    return Success(data=service.list_questions(dataset_id, page, page_size, is_ood))


@router.delete("/questions", summary="删除单个实验问题", tags=[TAG])
async def exp_delete_question(
    question_id: int = Query(..., description="问题 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    if not service.delete_question(question_id):
        return Fail(code=404, msg="问题不存在")
    return Success(data={"question_id": question_id}, msg="删除成功")


@router.post("/questions/clear", summary="清空实验问题（跟随当前筛选：全部/库内/OOD）", tags=[TAG])
async def exp_clear_questions(
    dataset_id: int = Query(..., description="数据集 ID"),
    is_ood: bool | None = Query(None, description="None=全部；false=仅库内题；true=仅 OOD 题"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    n = service.clear_questions(dataset_id, is_ood=is_ood)
    scope = "全部" if is_ood is None else ("OOD" if is_ood else "库内")
    return Success(data={"deleted": n}, msg=f"已清空 {scope} {n} 个问题")


# ---------------------------------------------------------------- runs


@router.post("/runs", summary="创建并启动实验运行（retrieval=消融对比；qa=端到端问答测评）", tags=[TAG])
async def exp_create_run(
    body: ExpRunCreate,
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    try:
        run = service.create_run(
            body.dataset_id,
            body.name,
            body.configs,
            body.question_limit,
            body.include_ood,
            created_by=current_user.id,
            kind=body.kind,
        )
    except ValueError as e:
        return Fail(code=400, msg=str(e))
    task_id = await exp_job.create_exp_run_job(run["id"])
    if not task_id:
        service.delete_run(run["id"])
        return Fail(code=503, msg="任务状态初始化失败（Redis 暂不可用），请稍后重试")
    return Success(data=run, msg="实验已启动")


@router.get("/runs", summary="实验运行列表", tags=[TAG])
async def exp_list_runs(
    dataset_id: int | None = Query(None, description="按数据集过滤"),
    kind: str | None = Query(None, description="按任务类型过滤（retrieval/qa）"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    runs = service.list_runs(dataset_id, kind=kind)
    active = {r["id"]: exp_job.get_exp_run_job_meta(r["id"]) for r in runs if r["status"] in ("queued", "running")}
    for r in runs:
        meta = exp_job.enrich_job_meta(active.get(r["id"]))
        if meta:
            r["progress"] = {
                "percent": meta.get("percent", 0),
                "done": meta.get("done"),
                "total": meta.get("total"),
                "stage": meta.get("stage"),
                "elapsed_seconds": meta.get("elapsed_seconds"),
            }
    return Success(data=runs)


@router.get("/runs/status", summary="查询实验运行进度", tags=[TAG])
async def exp_run_status(
    run_id: int = Query(..., description="运行 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    run = service.get_run(run_id)
    if not run:
        return Fail(code=404, msg="运行不存在")
    meta = exp_job.enrich_job_meta(exp_job.get_exp_run_job_meta(run_id))
    return Success(data={"run": run, "job": meta})


@router.post("/runs/cancel", summary="取消实验运行", tags=[TAG])
async def exp_run_cancel(
    run_id: int = Query(..., description="运行 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    run = service.get_run(run_id)
    if not run:
        return Fail(code=404, msg="运行不存在")
    if run["status"] not in ("queued", "running"):
        return Fail(code=400, msg="运行已结束，无法取消")
    await exp_job.request_exp_run_cancel(run_id)
    return Success(data={"run_id": run_id}, msg="已请求取消")


@router.get("/runs/results", summary="实验运行结果（配置聚合指标 + 逐题明细）", tags=[TAG])
async def exp_run_results(
    run_id: int = Query(..., description="运行 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    data = service.get_run_results(run_id)
    if not data:
        return Fail(code=404, msg="运行不存在")
    return Success(data=data)


@router.delete("/runs", summary="删除实验运行及其结果", tags=[TAG])
async def exp_delete_run(
    run_id: int = Query(..., description="运行 ID"),
    current_user: User = Depends(SuperuserControl.is_superuser),
):
    run = service.get_run(run_id)
    if not run:
        return Fail(code=404, msg="运行不存在")
    if run["status"] in ("queued", "running"):
        return Fail(code=400, msg="运行进行中，请先取消")
    service.delete_run(run_id)
    return Success(data={"run_id": run_id}, msg="删除成功")
