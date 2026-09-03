"""对话轮次开始时：在智能体调用前自动选档，将检索限定在相关 file_key（filename）子集或全库。"""

from __future__ import annotations

import textwrap
from typing import Any

from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field

from app.chat.tools import emit_rag_step
from app.kb.rag_utils import KB_MAX_DOCUMENT_FILTER
from app.settings import settings
from app.utils.egress import pinned_llm_client_kwargs


class _DocPickOut(BaseModel):
    file_keys: list[str] = Field(
        default_factory=list,
        description="从候选列表中选取的 file_key，须逐字一致；无需限定时为空列表。",
    )


def _pick_model(llm_config: dict[str, Any]):
    """
    选择模型，用于选档时调用。
    :param llm_config: 模型配置
    :return: 模型
    """
    api = (llm_config.get("api_key") or "").strip()
    if not api:
        return None
    name = (llm_config.get("model_name") or "gpt-4").strip()
    return init_chat_model(
        model=name,
        model_provider="openai",
        api_key=api,
        base_url=(llm_config.get("base_url") or "").strip() or None,
        temperature=0,
        stream_usage=False,
        **pinned_llm_client_kwargs((llm_config.get("base_url") or "").strip() or None),
    )


def run_kb_document_preselect(
    user_text: str,
    kb_scope: str,
    llm_config: dict[str, Any],
    *,
    conversation_context: str | None = None,
) -> tuple[list[str] | None, dict[str, Any]]:
    """
    返回 (document_filenames_for_rag, meta)。
    - None：不在 Milvus 侧加 filename 子句（全 knowledge scope 检索）。
    - 非空 list：仅在这些 filename 上检索。
    :param conversation_context: 可选的最近多轮对话文本，用于选档时消解指代。
    """
    meta: dict[str, Any] = {"stage": "kb_preselect"}
    ctx = (conversation_context or "").strip()
    meta["context_injected"] = bool(ctx)
    if ctx:
        meta["context_prompt_chars"] = len(ctx)
    if not settings.KB_DOCUMENT_PRESELECT_ENABLED:
        meta["skipped"] = "disabled_by_settings"
        emit_rag_step("📂", "知识库选档", "已关闭（KB_DOCUMENT_PRESELECT_ENABLED），全库检索")
        return None, meta

    try:
        from app.kb.kb_service import list_kb_filenames_for_scope

        names = list_kb_filenames_for_scope(kb_scope)
    except Exception as e:
        meta["error"] = str(e)[:200]
        emit_rag_step("⚠️", "知识库选档失败", meta["error"])
        return None, meta

    meta["candidate_count"] = len(names)
    if not names:
        emit_rag_step("📂", "知识库选档", "无已索引文档")
        return None, meta

    if len(names) == 1:
        meta["reason"] = "single_document_autopick"
        meta["resolved_file_keys"] = names
        emit_rag_step("📂", "知识库选档", "仅 1 份文档，自动限定")
        return [names[0]], meta

    # 选择模型，用于选档时调用。
    model = _pick_model(llm_config)
    if not model:
        meta["skipped"] = "no_api_key"
        emit_rag_step("📂", "知识库选档", "无有效 API Key，全库检索")
        return None, meta

    max_lines = min(100, max(20, int(settings.KB_PRESELECT_MAX_DOC_LINES or 100)))
    display = names[:max_lines]
    tail = ""
    if len(names) > len(display):
        tail = f"\n\n（知识库共 {len(names)} 份文档，此处仅列出前 {len(display)} 份；file_key 仍必须逐字来自完整集合。）"

    # 构建选档提示词
    catalog = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(display))
    qmax = max(100, int(getattr(settings, "KB_PRESELECT_MAX_CURRENT_QUESTION_CHARS", 8000) or 8000))
    q = (user_text or "").strip()[:qmax]
    if ctx:
        user_block = f"{ctx}\n\n【当前用户问题】\n{q}"
    else:
        user_block = q
    prompt = textwrap.dedent(
        f"""\
        你是知识库路由助手。根据「用户问题」从下方「文档 file_key」列表中，选出与问题**直接相关**的文档。
        若上方提供了前序对话，仅用于理解指代与主题；**选档仍以「当前用户问题」为主**（无单独标题时，整段即用户问题）。
        规则：
        - file_key 必须从列表中**原样复制**，不得编造、缩写或改扩展名。
        - 最多选 {KB_MAX_DOCUMENT_FILTER} 个；若问题需要通览多份材料、或无法判断、或应全库检索，则返回空数组 []。
        - 只输出 JSON（字段 file_keys: 字符串数组）。提示中须出现 json 字样以兼容接口。

        用户问题与上下文：
        {user_block}

        文档 file_key 列表：
        {catalog}
        {tail}
        """
    )

    # 调用模型选档
    try:
        resp = model.with_structured_output(_DocPickOut, method="json_mode").invoke(  # type: ignore[union-attr]
            [{"role": "user", "content": prompt}]
        )
    except Exception as e:
        meta["error"] = f"llm_preselect: {e}"[:200]
        emit_rag_step("⚠️", "知识库选档模型失败", meta["error"])
        return None, {**meta, "reason": "llm_error_fallback_full"}

    allow = set(names) # 允许的 file_key 集合
    picked: list[str] = [] # 选中的 file_key 列表
    for k in resp.file_keys or []:
        s = (k or "").strip()
        if s in allow and s not in picked:
            picked.append(s)
    picked = picked[:KB_MAX_DOCUMENT_FILTER]
    meta["llm_raw_count"] = len(resp.file_keys or [])
    if not picked:
        meta["reason"] = "llm_empty_or_unmatched_full_scope"
        emit_rag_step("📂", "知识库选档", "模型未圈定或未能匹配，按全知识库范围检索")
        return None, meta

    meta["reason"] = "llm_picked"
    meta["resolved_file_keys"] = picked
    emit_rag_step("📂", "知识库选档", f"已限定 {len(picked)} 个 file_key")
    return picked, meta
