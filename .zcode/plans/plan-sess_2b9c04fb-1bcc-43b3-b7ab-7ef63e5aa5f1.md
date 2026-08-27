# Agentic RAG 改造方案（不含查询重写部分）

范围：分块与参数、向量存储（BM25 迁移）、检索质量门控、生成出口轻量防幻觉、知识库更新。**不改动**：查询重写四法、Agent 循环/迭代策略（第 4 大点）。

---

## 1. 分块：参数校正 + Excel 表格专项

**1a. 块大小与 merge 阈值校正（`app/kb/multimodal_document_loader.py` + `app/settings/config.py`）**
- `MultimodalDocumentLoader` 构造函数默认值 `chunk_size: 500 → 900`、`chunk_overlap: 50 → 90`，推导结果：L1=1800 / L2=900 / L3=450 / L4 图片不变。三级分隔符列表保持不变。
- `AUTO_MERGE_THRESHOLD` 默认 `2 → 3`（config.py:191）。
- 显式说明：参数只对新上传文档生效，旧文档需重新上传后才按新粒度重建。

**1b. Excel 表格专项（`app/kb/multimodal_document_loader.py` load_document）**
- .xlsx/.xls 分支不再把 `UnstructuredExcelLoader` 的原始输出直接喂给文本切分器，改为：逐 sheet 读取 → 每 sheet 转成 Markdown 表格文本（含表头行），sheet 序号作为 `page_number`；表头过大时并入后续行对。整表作为该"页"的内容进入现有 L1/L2/L3 切分——分离符优先级里 `\n` 使表格按行边界切分，行不会被拦腰截断（表格"整块保留"以行为单位）。
- 图片提取逻辑不变（Excel 无图片）。

## 2. 向量存储：迁移到 Milvus 服务端 BM25

**2a. 集合 schema 增加 BM25 Function（`app/kb/milvus_client.py` init_collection）**
- 新增 `Function(function_type=FunctionType.BM25, input_field_names=["text"], output_field_names=["bm25_sparse"], params={"analyzer_params": {"type": "chinese"}})`（pymilvus 2.6 / Milvus 2.5.14 均支持）。
- 新建集合改为 `auto_id=False` 以便迁移复制原 id。

**2b. 检索改造（`app/kb/milvus_client.py` hybrid_retrieve）**
- 签名从 `sparse_embedding: dict` 改为 `query_text: str`；稀疏腿改为 `AnnSearchRequest(data=[query_text], anns_field="bm25_sparse", function_name="bm25_fn")`，与 dense 腿同样 RRF(k=60) 融合；`expr` 前置过滤保持不变。
- dense fallback 保留（hybrid 失败时仍可退纯 dense）。

**2c. 调用侧清理（`app/kb/rag_utils.py` + `app/kb/multimodal_milvus_writer.py`）**
- `retrieve_documents` 不再调 `fit_corpus` / `get_sparse_embedding`，直接传 query 文本。
- writer 不再生成 sparse：插入只带 dense + 标量字段（sparse 由服务端 Function 自动计算）。图片块 text="" 生成的空稀疏向量不影响检索。
- `multimodal_embedding.py` 的 BM25 方法保留不变（先核对 `app/chat/memory_*` 会话记忆路径是否引用 fit_corpus；若引用则保留，否则可后续清理，本方案不动会话记忆）。

**2d. 一键迁移脚本（新文件 `app/kb/migrate_bm25.py`，`python -m app.kb.migrate_bm25`）**
- 检测 `kura_ai_kb` 是否已含 bm25 Function → 已迁移则跳过（幂等）。
- 迁移流程：建 `kura_ai_kb_v2`（含 Function、auto_id=False）→ 用 query_iterator 分页读旧集合全部行（output_fields 含 `dense_embedding` 与全部标量，**直读复制、不重调 embedding API、零 API 费用**）→ 批量插入 v2 → drop 旧集合 → `rename_collection` 为 `kura_ai_kb` → 打印迁移统计。
- 备份提示：脚本执行前输出旧集合行数与提示；一次 5 分钟内完成（数据量级）。

## 3. 检索质量门控（`app/kb/rag_pipeline.py` + `app/kb/search_tool.py`）

**3a. 逐块打分替换整段打分**
- `grade_documents_node`：新增结构化输出 schema `ChunkGrades(ratings: list[{chunk_index, binary_score}])`，一次 LLM 调用对格式化后的 top-5 逐块打分；通过条件为"任一 chunk yes"（不再是整段 yes/no）。新增设置 `KB_GRADE_RATINGS_MODEL` 复用现有 `RAG_GRADE_MODEL` 缺省回退逻辑。
- 保留原有 `grade_route` 上报字段（rag_trace 兼容前端）。

**3b. 扩展检索后的二次门控 + 拒答路径**
- 新增节点 `grade_expanded`（复用同一个逐块 grader）：`retrieve_expanded` 之后打分；全部 no 或 docs 为空 → route 到 `no_answer`（END），state 置 `no_answer: True` 并记录 trace。
- `search_tool.py` 收到 `no_answer=True` 时返回固定文案："知识库经两次检索与相关性评估，未找到相关资料。请如实告知用户：知识库中没有相关资料，可建议其补充资料；禁止编造任何知识库结论。"（配合 §4 提示词纪律）。
- 新设置：`KB_GRADE_REFUSAL_ENABLED=True`（可关，关时保持旧行为直接生成）。

**3c. rerank 分数阈值（可选，默认关闭）**
- `rag_utils._rerank_documents` 之后若 max(rerank_score) < `RERANK_MIN_SCORE`（新设置，默认 None=禁用）则将 meta 标记为不达标，图中走 no_answer。当前环境未配置 rerank，此开关为前瞻预留。

## 4. 生成出口：轻量防幻觉组合

**4a. 提示词作答纪律（`app/chat/agent_service.py` `_compose_system_prompt`）**
- `use_knowledge_retrieval` 分支追加：回答必须仅依据检索结果与多轮上下文；凡引用检索内容须以 `[来源N]` 标注（N 与工具输出编号一致）；检索未命中或工具明确提示无相关资料时，必须如实告知"知识库中未找到相关资料"并说明可补充，不得编造；区分知识库结论与一般常识推断。
- `search_tool.py` 工具 description 同步加一句"引用时必须使用 [来源N] 编号"。

**4b. 来源数据贯通（kb_sources）**
- `kb_tool_formatting.format_knowledge_retrieval_tool_output` 增加第三返回值 `kb_sources: list[{index, filename, page_number, chunk_id, score, content_type, image_url?}]`（index 与 [i] 对齐）；`search_tool.py` 与 `image_search_tool.py` 写入 `_set_last_rag_context({"kb_sources": ...})`。

**4c. 输出与落库（`agent_service.py` 同步/流式两处 + `storage.py` + `db_models.py`）**
- sync：返回值加 `"sources"`；stream：在 `trace` 事件后新增 `{"type":"sources","sources":[...]}` 事件；两处 `extra_message_data` 加 `sources`。
- `ChatMessage` 加 `sources JSON` 列（照 image_references 先例：`db_models.py` + `database.py` 的 `ALTER TABLE ADD COLUMN IF NOT EXISTS sources JSON`）。
- `storage.save` 提取/写入 sources；`storage.get_session_messages` 回填 sources（顺带修复 `image_references` 同样的读回缺口）。
- `schemas/agent_chat.py` 的 `ChatResponse`、`MessageInfo` 加 `sources`；`api/v1/user_agents/chat.py` 历史接口 MessageInfo 构造处带上。

**4d. 前端来源列表（`web/src/views/agent-chat/index.vue`）**
- `applyChatSsePayload` 加 `sources` 事件分支；助手气泡下方渲染来源小列表（"来源：[1] 文件名 · P页码"）；历史加载映射 `row.sources`；i18n 加"来源/Sources"文案。编辑器试聊面板（`previewChatSse.js`）同步事件解析（可不渲染，仅存状态）。

## 5. 知识库更新：内容 hash 抠查（`kb_service.py` + `db_models.py`）

- `KbDocument` 加 `content_hash String(64)` 列（`database.py` 同步 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`）。
- `ingest_upload`：读文件字节后先 `sha256`；查询同 (kb_scope, display_filename) 旧行，hash 相同 → **直接返回现有元数据（unchanged=True），不删不重建**；不同或无旧行 → 走现有"先删后增"流程并在插入时写 content_hash。
- 响应结构可加 `unchanged` 标记（schema `KbUploadResponse` 加可选字段），其余接口不变。

## 6. 实施顺序与验证

1. **门控 + 提示词纪律**（§3+§4a，纯逻辑无迁移）→ 用现有 UI 测试账号 + test 智能体跑通：检查 rag_steps 出现逐块评分、无相关文档时返回拒答文案。
2. **BM25 迁移**（§2）→ `python -m app.kb.migrate_bm25` 迁移 > 检索 smoke test（英文型号词、产品名精确匹配对照迁移前）。
3. **sources 链路 + 前端**（§4b-d）→ SSE 事件、落库、历史回放逐级验证。
4. **分块参数 + Excel + hash**（§1+§5）→ 上传 PDF/Excel 验证 chunk_count 变化、Excel 表格 Markdown 块；同内容重传返回 unchanged。

已知边界：分块参数仅对新上传文档生效（旧文档需重传）；BM25 迁移存在一次性短窗口（脚本幂等、可重跑）；chinese analyzer 与现单字切词行为略有差异，以 smoke test 结果为准。