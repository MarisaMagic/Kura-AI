"""LangGraph RAG 子图：检索 → 打分 → 可选扩展检索。"""

from __future__ import annotations

from typing import Any, List, Literal, Optional, TypedDict

from langchain.chat_models import init_chat_model
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from app.kb.rag_utils import (
    generate_hypothetical_document,
    retrieve_documents,
    step_back_expand,
)
from app.settings import settings


class GradeDocuments(BaseModel):
    binary_score: str = Field(description="'yes' or 'no'")


class RewriteStrategy(BaseModel):
    strategy: Literal["step_back", "hyde", "complex"]


class RAGState(TypedDict):
    question: str
    kb_scope: str
    llm_config: dict[str, Any]
    query: str
    context: str
    docs: List[dict]
    route: Optional[str]
    expansion_type: Optional[str]
    expanded_query: Optional[str]
    step_back_question: Optional[str]
    step_back_answer: Optional[str]
    hypothetical_doc: Optional[str]
    rag_trace: Optional[dict]


def _format_docs(docs: List[dict]) -> str:
    """
    格式化文档, 将检索到的文档列表格式化为字符串。
    :param docs: 文档列表
    :return: 格式化后的文档
    """
    if not docs:
        return ""
    chunks = []
    for i, doc in enumerate(docs, 1):
        source = doc.get("filename", "Unknown")
        page = doc.get("page_number", "N/A")
        text = doc.get("text", "")
        chunks.append(f"[{i}] {source} (Page {page}):\n{text}")
    return "\n\n---\n\n".join(chunks)


def _grader_model(llm_config: dict[str, Any] | None):
    """
    初始化评分模型, 用于评估检索到的文档是否与用户问题相关。
    :param llm_config: 聊天模型配置
    :return: 评分模型
    """
    if not llm_config or not (llm_config.get("api_key") or "").strip():
        return None
    grade_model = (getattr(settings, "RAG_GRADE_MODEL", None) or "").strip() or None
    name = grade_model or (llm_config.get("model_name") or "gpt-4")
    return init_chat_model(
        model=name,
        model_provider="openai",
        api_key=(llm_config.get("api_key") or "").strip(),
        base_url=(llm_config.get("base_url") or "").strip() or None,
        temperature=0,
        # Avoid empty/malformed tool-call payloads with OpenAI-compatible gateways (e.g. DashScope).
        stream_usage=False,
    )


def _router_model(llm_config: dict[str, Any] | None):
    """
    初始化路由模型, 用于选择最合适的查询扩展策略（step_back、hyde、complex）。
    :param llm_config: 聊天模型配置
    :return: 路由模型
    """
    if not llm_config or not (llm_config.get("api_key") or "").strip():
        return None
    return init_chat_model(
        model=llm_config.get("model_name") or "gpt-4",
        model_provider="openai",
        api_key=(llm_config.get("api_key") or "").strip(),
        base_url=(llm_config.get("base_url") or "").strip() or None,
        temperature=0,
        stream_usage=False,
    )


GRADE_PROMPT = (
    "You are a grader assessing relevance of a retrieved document to a user question. \n "
    "Here is the retrieved document: \n\n {context} \n\n"
    "Here is the user question: {question} \n"
    "If the document contains keyword(s) or semantic meaning related to the user question, grade it as relevant. \n"
    "Give a binary score 'yes' or 'no' score to indicate whether the document is relevant to the question.\n"
    "Reply with JSON only (field binary_score: 'yes' or 'no'). "
    "DashScope json_object mode requires the word json in the prompt."
)


def retrieve_initial(state: RAGState) -> RAGState:
    from app.chat.tools import emit_rag_step

    """
    初始化检索, 用于检索与用户问题相关的文档。
    :param state: 状态
    :return: 检索后的文档列表和检索的元数据
    """
    query = state["question"] # 用户问题
    kb_scope = state["kb_scope"] # 当前智能体检索的知识库范围
    emit_rag_step("🔍", "正在检索知识库...", f"查询: {query[:50]}")
    retrieved = retrieve_documents(query, kb_scope=kb_scope, top_k=5) # 检索与用户问题相关的文档
    results = retrieved.get("docs", []) # 检索到的文档列表
    retrieve_meta = retrieved.get("meta", {}) # 检索的元数据
    context = _format_docs(results) # 格式化检索到的文档列表
    emit_rag_step(
        "🧱",
        "三级分块检索",
        (
            f"叶子层 L{retrieve_meta.get('leaf_retrieve_level', 3)} 召回，"
            f"候选 {retrieve_meta.get('candidate_k', 0)}"
        ),
    )
    emit_rag_step(
        "🧩",
        "Auto-merging 合并",
        (
            f"启用: {bool(retrieve_meta.get('auto_merge_enabled'))}，"
            f"应用: {bool(retrieve_meta.get('auto_merge_applied'))}，"
            f"替换片段: {retrieve_meta.get('auto_merge_replaced_chunks', 0)}"
        ),
    )
    emit_rag_step("✅", f"检索完成，找到 {len(results)} 个片段", f"模式: {retrieve_meta.get('retrieval_mode', 'hybrid')}")
    rag_trace = { # 检索的元数据
        "tool_used": True,
        "tool_name": "search_knowledge_base",
        "kb_scope": kb_scope,
        "query": query,
        "expanded_query": query,
        "initial_empty": len(results) == 0,
        "retrieved_chunks": results,
        "initial_retrieved_chunks": results,
        "retrieval_stage": "initial",
        "rerank_enabled": retrieve_meta.get("rerank_enabled"),
        "rerank_applied": retrieve_meta.get("rerank_applied"),
        "rerank_model": retrieve_meta.get("rerank_model"),
        "rerank_endpoint": retrieve_meta.get("rerank_endpoint"),
        "rerank_error": retrieve_meta.get("rerank_error"),
        "retrieval_mode": retrieve_meta.get("retrieval_mode"),
        "candidate_k": retrieve_meta.get("candidate_k"),
        "leaf_retrieve_level": retrieve_meta.get("leaf_retrieve_level"),
        "auto_merge_enabled": retrieve_meta.get("auto_merge_enabled"),
        "auto_merge_applied": retrieve_meta.get("auto_merge_applied"),
        "auto_merge_threshold": retrieve_meta.get("auto_merge_threshold"),
        "auto_merge_replaced_chunks": retrieve_meta.get("auto_merge_replaced_chunks"),
        "auto_merge_steps": retrieve_meta.get("auto_merge_steps"),
    }
    # 返回检索后的文档列表和检索的元数据
    return { 
        "query": query, # 用户问题
        "docs": results, # 检索到的文档列表
        "context": context, # 格式化后的检索到的文档列表
        "rag_trace": rag_trace, # 检索的元数据
    }


def grade_documents_node(state: RAGState) -> RAGState:
    from app.chat.tools import emit_rag_step

    """
    评估文档相关性, 用于评估检索到的文档是否与用户问题相关。
    :param state: 状态
    :return: 评估后的文档列表和评估的元数据
    """
    llm_config = state.get("llm_config") or {} # 聊天模型配置
    grader = _grader_model(llm_config) # 评分模型
    emit_rag_step("📊", "正在评估文档相关性...")
    if not grader:
        grade_update = {
            "grade_score": "unknown",
            "grade_route": "rewrite_question",
            "rewrite_needed": True,
        }
        rag_trace = state.get("rag_trace", {}) or {}
        rag_trace.update(grade_update)
        return {"route": "rewrite_question", "rag_trace": rag_trace}
    # 评估文档相关性
    question = state["question"] # 用户问题
    context = state.get("context", "") # 格式化后的检索到的文档列表
    prompt = GRADE_PROMPT.format(question=question, context=context) # 评估文档相关性的提示
    response = grader.with_structured_output(GradeDocuments, method="json_mode").invoke( # 评估文档相关性
        [{"role": "user", "content": prompt}] 
    )
    score = (response.binary_score or "").strip().lower() 
    route = "generate_answer" if score == "yes" else "rewrite_question" # 如果评分是 yes，则返回生成答案，否则返回重写查询
    if route == "generate_answer":
        emit_rag_step("✅", "文档相关性评估通过", f"评分: {score}")
    else:
        emit_rag_step("⚠️", "文档相关性不足，将重写查询", f"评分: {score}")
    grade_update = {
        "grade_score": score,
        "grade_route": route,
        "rewrite_needed": route == "rewrite_question",
    }
    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update(grade_update)
    return {"route": route, "rag_trace": rag_trace}


def rewrite_question_node(state: RAGState) -> RAGState:
    from app.chat.tools import emit_rag_step

    """
    重写查询, 用于重写用户问题，选择最合适的查询扩展策略（step_back、hyde、complex）。
    :param state: 状态
    :return: 重写后的查询扩展策略
    """
    question = state["question"]
    llm_config = state.get("llm_config") or {}
    emit_rag_step("✏️", "正在重写查询...")
    router = _router_model(llm_config) # 路由模型
    strategy = "step_back"
    if router:
        prompt = (
            "请根据用户问题选择最合适的查询扩展策略。\n"
            "- step_back：包含具体名称、日期、代码等细节，需要先理解通用概念的问题。\n"
            "- hyde：模糊、概念性、需要解释或定义的问题。\n"
            "- complex：多步骤、需要分解或综合多种信息的复杂问题。\n"
            f"用户问题：{question}\n"
            "用 JSON 输出 strategy 字段，取值必须是 step_back、hyde、complex 之一。"
            "（兼容接口要求提示中出现 json 字样。）"
        )
        try:
            decision = router.with_structured_output(RewriteStrategy, method="json_mode").invoke( # 选择最合适的查询扩展策略
                [{"role": "user", "content": prompt}]
            )
            strategy = decision.strategy
        except Exception:
            strategy = "step_back"

    expanded_query = question
    step_back_question = ""
    step_back_answer = ""
    hypothetical_doc = ""
    
    # 如果查询扩展策略是 step_back 或 complex，则生成退步问题
    if strategy in ("step_back", "complex"):
        emit_rag_step("🧠", f"使用策略: {strategy}", "生成退步问题")
        step_back = step_back_expand(question, llm_config)
        step_back_question = step_back.get("step_back_question", "")
        step_back_answer = step_back.get("step_back_answer", "")
        expanded_query = step_back.get("expanded_query", question)

    # 如果查询扩展策略是 hyde 或 complex，则生成假设性文档
    if strategy in ("hyde", "complex"):
        emit_rag_step("📝", "HyDE 假设性文档生成中...")
        hypothetical_doc = generate_hypothetical_document(question, llm_config)

    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update(
        {
            "rewrite_strategy": strategy,
            "rewrite_query": expanded_query,
        }
    )

    return {
        "expansion_type": strategy,
        "expanded_query": expanded_query,
        "step_back_question": step_back_question,
        "step_back_answer": step_back_answer,
        "hypothetical_doc": hypothetical_doc,
        "rag_trace": rag_trace,
    }


def retrieve_expanded(state: RAGState) -> RAGState:
    from app.chat.tools import emit_rag_step

    """
    扩展查询, 用于扩展用户问题，选择最合适的查询扩展策略（step_back、hyde、complex）。
    :param state: 状态
    :return: 扩展后的查询扩展策略
    """
    kb_scope = state["kb_scope"] # 当前智能体检索的知识库范围
    strategy = state.get("expansion_type") or "step_back" # 查询扩展策略
    emit_rag_step("🔄", "使用扩展查询重新检索...", f"策略: {strategy}")
    results: List[dict] = []
    rerank_applied_any = False
    rerank_enabled_any = False
    rerank_model = None
    rerank_endpoint = None
    rerank_errors: list[str] = []
    retrieval_mode = None
    candidate_k = None
    leaf_retrieve_level = None
    auto_merge_enabled = None
    auto_merge_applied = False
    auto_merge_threshold = None
    auto_merge_replaced_chunks = 0
    auto_merge_steps = 0

    hyde_chunk_count = 0
    # 如果查询扩展策略是 hyde 或 complex，则检索和假设性文档相关的文档
    if strategy in ("hyde", "complex"):
        hypothetical_doc = state.get("hypothetical_doc") or generate_hypothetical_document( # 生成假设性文档
            state["question"], state.get("llm_config")
        )
        retrieved_hyde = retrieve_documents(hypothetical_doc, kb_scope=kb_scope, top_k=5) # 检索和假设性文档相关的文档
        hyde_docs = retrieved_hyde.get("docs", [])
        hyde_chunk_count = len(hyde_docs)
        results.extend(hyde_docs)
        hyde_meta = retrieved_hyde.get("meta", {})
        emit_rag_step(
            "🧱",
            "HyDE 三级检索",
            (
                f"L{hyde_meta.get('leaf_retrieve_level', 3)} 召回，"
                f"候选 {hyde_meta.get('candidate_k', 0)}，"
                f"合并替换 {hyde_meta.get('auto_merge_replaced_chunks', 0)}"
            ),
        )
        rerank_applied_any = rerank_applied_any or bool(hyde_meta.get("rerank_applied"))
        rerank_enabled_any = rerank_enabled_any or bool(hyde_meta.get("rerank_enabled"))
        rerank_model = rerank_model or hyde_meta.get("rerank_model")
        rerank_endpoint = rerank_endpoint or hyde_meta.get("rerank_endpoint")
        if hyde_meta.get("rerank_error"):
            rerank_errors.append(f"hyde:{hyde_meta.get('rerank_error')}")
        retrieval_mode = retrieval_mode or hyde_meta.get("retrieval_mode")
        candidate_k = candidate_k or hyde_meta.get("candidate_k")
        leaf_retrieve_level = leaf_retrieve_level or hyde_meta.get("leaf_retrieve_level")
        auto_merge_enabled = auto_merge_enabled if auto_merge_enabled is not None else hyde_meta.get("auto_merge_enabled")
        auto_merge_applied = auto_merge_applied or bool(hyde_meta.get("auto_merge_applied"))
        auto_merge_threshold = auto_merge_threshold or hyde_meta.get("auto_merge_threshold")
        auto_merge_replaced_chunks += int(hyde_meta.get("auto_merge_replaced_chunks") or 0)
        auto_merge_steps += int(hyde_meta.get("auto_merge_steps") or 0)

    # complex：第二次检索（HyDE）仍为 0 条时不再做 Step-back，避免第三次向量检索
    skip_step_back = strategy == "complex" and hyde_chunk_count == 0
    if skip_step_back:
        emit_rag_step(
            "⏭️",
            "已结束扩展检索",
            "第二次检索（HyDE）无片段，跳过 Step-back 检索",
        )

    # 如果查询扩展策略是 step_back 或 complex，并且不跳过退步检索，则检索和退步问题相关的文档
    if strategy in ("step_back", "complex") and not skip_step_back:
        expanded_query = state.get("expanded_query") or state["question"] # 扩展后的查询
        retrieved_stepback = retrieve_documents(expanded_query, kb_scope=kb_scope, top_k=5) # 检索和退步问题相关的文档
        results.extend(retrieved_stepback.get("docs", []))
        step_meta = retrieved_stepback.get("meta", {})
        emit_rag_step(
            "🧱",
            "Step-back 三级检索",
            (
                f"L{step_meta.get('leaf_retrieve_level', 3)} 召回，"
                f"候选 {step_meta.get('candidate_k', 0)}，"
                f"合并替换 {step_meta.get('auto_merge_replaced_chunks', 0)}"
            ),
        )
        rerank_applied_any = rerank_applied_any or bool(step_meta.get("rerank_applied"))
        rerank_enabled_any = rerank_enabled_any or bool(step_meta.get("rerank_enabled"))
        rerank_model = rerank_model or step_meta.get("rerank_model")
        rerank_endpoint = rerank_endpoint or step_meta.get("rerank_endpoint")
        if step_meta.get("rerank_error"):
            rerank_errors.append(f"step_back:{step_meta.get('rerank_error')}")
        retrieval_mode = retrieval_mode or step_meta.get("retrieval_mode")
        candidate_k = candidate_k or step_meta.get("candidate_k")
        leaf_retrieve_level = leaf_retrieve_level or step_meta.get("leaf_retrieve_level")
        auto_merge_enabled = auto_merge_enabled if auto_merge_enabled is not None else step_meta.get("auto_merge_enabled")
        auto_merge_applied = auto_merge_applied or bool(step_meta.get("auto_merge_applied"))
        auto_merge_threshold = auto_merge_threshold or step_meta.get("auto_merge_threshold")
        auto_merge_replaced_chunks += int(step_meta.get("auto_merge_replaced_chunks") or 0)
        auto_merge_steps += int(step_meta.get("auto_merge_steps") or 0)

    deduped = []
    seen = set()
    for item in results:
        key = (item.get("filename"), item.get("page_number"), item.get("text"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    for idx, item in enumerate(deduped, 1):
        item["rrf_rank"] = idx

    context = _format_docs(deduped) # 格式化扩展检索到的文档列表
    emit_rag_step("✅", f"扩展检索完成，共 {len(deduped)} 个片段")
    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update(
        {
            "expanded_query": state.get("expanded_query") or state["question"],
            "step_back_question": state.get("step_back_question", ""),
            "step_back_answer": state.get("step_back_answer", ""),
            "hypothetical_doc": state.get("hypothetical_doc", ""),
            "expansion_type": strategy,
            "skipped_step_back_after_empty_hyde": bool(skip_step_back),
            "retrieved_chunks": deduped,
            "expanded_retrieved_chunks": deduped,
            "retrieval_stage": "expanded",
            "rerank_enabled": rerank_enabled_any,
            "rerank_applied": rerank_applied_any,
            "rerank_model": rerank_model,
            "rerank_endpoint": rerank_endpoint,
            "rerank_error": "; ".join(rerank_errors) if rerank_errors else None,
            "retrieval_mode": retrieval_mode,
            "candidate_k": candidate_k,
            "leaf_retrieve_level": leaf_retrieve_level,
            "auto_merge_enabled": auto_merge_enabled,
            "auto_merge_applied": auto_merge_applied,
            "auto_merge_threshold": auto_merge_threshold,
            "auto_merge_replaced_chunks": auto_merge_replaced_chunks,
            "auto_merge_steps": auto_merge_steps,
        }
    )
    return {"docs": deduped, "context": context, "rag_trace": rag_trace}


def build_rag_graph():
    """
    构建 RAG 子图, 用于构建 RAG 子图。
    :return: RAG 子图
    """
    graph = StateGraph(RAGState)
    graph.add_node("retrieve_initial", retrieve_initial)
    graph.add_node("grade_documents", grade_documents_node)
    graph.add_node("rewrite_question", rewrite_question_node)
    graph.add_node("retrieve_expanded", retrieve_expanded)

    graph.set_entry_point("retrieve_initial")

    # 用原始问题进行一次检索
    def _route_after_initial(state: RAGState) -> str:
        docs = state.get("docs") or []
        return "rewrite_question" if len(docs) == 0 else "grade_documents"

    graph.add_conditional_edges(
        "retrieve_initial",
        _route_after_initial,
        {
            # 如果检索到的文档列表为空，则重写查询
            "rewrite_question": "rewrite_question",
            # 如果检索到的文档列表不为空，则评估文档相关性
            "grade_documents": "grade_documents",
        },
    )
    graph.add_conditional_edges(
        "grade_documents",
        lambda state: state.get("route"),
        {
            # 如果评估文档相关性为是，则生成答案
            "generate_answer": END,
            # 如果评估文档相关性为否，则重写查询
            "rewrite_question": "rewrite_question",
        },
    )
    # 如果重写查询，则扩展查询
    graph.add_edge("rewrite_question", "retrieve_expanded")
    # 如果扩展查询，则结束
    graph.add_edge("retrieve_expanded", END)
    return graph.compile()


rag_graph = build_rag_graph()


def run_rag_graph(question: str, kb_scope: str, llm_config: dict[str, Any]) -> dict:
    """
    运行 RAG 子图, 用于运行 RAG 子图。
    :param question: 用户问题
    :param kb_scope: 当前智能体检索的知识库范围
    :param llm_config: 聊天模型配置
    :return: RAG 子图结果
    """
    return rag_graph.invoke( # 运行 RAG 子图
        {
            "question": question,
            "kb_scope": kb_scope,
            "llm_config": llm_config,
            "query": question,
            "context": "",
            "docs": [],
            "route": None,
            "expansion_type": None,
            "expanded_query": None,
            "step_back_question": None,
            "step_back_answer": None,
            "hypothetical_doc": None,
            "rag_trace": None,
        }
    )
