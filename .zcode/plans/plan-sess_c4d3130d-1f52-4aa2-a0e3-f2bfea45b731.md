# 工具调用前过渡文本 → 思考面板（改造方案）

## 结论（问题的本质）
“我来检索知识库中…的具体内容。”不是真正的推理内容（模型没返回 `reasoning_content`），而是模型决定调用工具那一轮补全首先生成的普通正文（tool-call preamble）。目前它和正式回答走同一个 `type=content` 事件流入正文气泡；工具调用参数 chunk 在 `agent_service.py:649` 被静默丢弃，前端收不到“开始检索”信号；执行 RAG 期间同步阻塞、无任何输出，于是出现“说一句 → 长停顿 → 又一大段”。前端在收到首个 content 后把 `pending` 置 false，停顿期间连“思考中…”都没了。

## 后端改动

### 1. `app/chat/agent_service.py` — `_agent_worker` 流式分类（L635-664 附近）
- 抽出文本提取小函数（str / list 块的 type=="text" 分支，现有逻辑照搬）。
- 跟踪状态：`current_msg_id`、`msg_text_emitted`（当前 AI 消息里已按 content 发出的文本）、`msg_moved`（该消息是否已被判定为工具调用）、`thinking_text_parts`（全部思考文本，用于落库）、`full_response`（改为只存正式回答正文）。
- 每个 AIMessageChunk 的处理规则：
  - `msg.id` 变化时按新消息重置 `msg_text_emitted`/`msg_moved`（id 为 None 时走降级：只在工具调用出现时搬迁已发出文本，不进入“思考直发”模式，最坏情况退化为现状）。
  - 检测到 `tool_call_chunks` 时：若本消息已发出文本，先发 `{"type":"thinking_move","text": <该消息已发出的全部文本>}`（一次性把前导句从正文迁出），并把该文本计入 `thinking_text_parts`；随后本消息内剩余文本改发 `{"type":"thinking_text","content":...}`。
  - 无工具调用的文本：照旧累积 `full_response` 并发 `content` 事件（正式回答不受影响，逐 token 流式）。
  - 正确处理“同一 chunk 同时带文本和 tool_call_chunks”的情况（先 move 再 thinking_text）。
  - 多轮工具调用靠 `msg.id` 变化天然支持（第二次工具的前导句同样会被移出）。
- 落库段（L718-732）：`messages.append(AIMessage(content=full_response))` 不变（此时只剩正式回答）；`extra_message_data[-1]` 增加 `"thinking_text": "".join(thinking_text_parts) or None`。
- `chat_with_agent_sync`（L366）经核实只返回最后一条 AI 消息内容，不受影响——不改。

### 2. 持久化（拆分存储）
- `app/chat/db_models.py` `ChatMessage`：新增可空列 `thinking_text: Mapped[str | None] = mapped_column(Text, nullable=True)`（放在 `sources` 旁，更新类 docstring）。
- `app/chat/database.py` `init_chat_db`：在现有迁移块追加 `ALTER TABLE mg_chat_messages ADD COLUMN IF NOT EXISTS thinking_text TEXT`。
- `app/chat/storage.py`：
  - `save`：从 `extra_message_data` 读 `thinking_text`，写入 ChatMessageRow 及 Redis 序列化 dict。
  - `get_session_messages` 的 DB 兜底路径（L386-401）：dict 中加 `"thinking_text": row.thinking_text`。
- `app/schemas/agent_chat.py` `MessageInfo`：加 `thinking_text: Optional[str] = None`。
- `app/api/v1/user_agents/chat.py` L513-522：MessageInfo 构造补 `thinking_text=m.get("thinking_text")`。

注：旧消息不做回迁拆分（历史既有记录仍混在正文里，可接受）；`chat_job.py` 的 Redis 事件重放是 dict 透传，新事件类型自动可用，无需改。

## 前端改动

### 3. `web/src/views/agent-chat/index.vue`
- `applyChatSsePayload`（L629-693）新增两个分支：
  - `thinking_move`：把 `data.text` 从当前 `content` 尾部移除（`endsWith` 校验后 slice，兜底不做移除），追加进新字段 `thinkingText`；置 `thinkingOpen: true`、`pending: true`（恢复“思考中”状态，正文隐藏、灯泡 pill 显示思考中，正好覆盖工具执行的停顿期）。
  - `thinking_text`：`thinkingText += data.content`，`thinkingOpen: true`，pending 保持不动。
  - `content` 分支补 `thinkingText: row.thinkingText ?? ''`（其余分支靠 spread 保持）。
- 模板思考面板（L103-119）：在 ragSteps 列表与 placeholder 之间新增“思考过程”小节（`v-if="(m.thinkingText||'').trim()"`）：小标签（i18n key）+ `v-html="renderAgentChatMarkdown(m.thinkingText)"`；placeholder 的 `v-else` 条件改为“无 ragSteps 且无 thinkingText 时才显示”。
- `loadMessagesForSession`（L956-983）：行映射加 `thinkingText: row.thinking_text || ''`。
- 提交消息时新建的助手占位行补 `thinkingText: ''` 初始化。
- scoped CSS 添加 `.agent-chat-thinking-text` 少量样式（与现有 thinking panel 配色一致）。

### 4. `web/src/views/agents/composables/previewChatSse.js`
- `applyPreviewChatSsePayload` 加同样的 `thinking_move` / `thinking_text` 两个分支。

### 5. `web/src/views/agents/components/AgentEditorPreviewPanel.vue`
- 思考面板模板同步加“思考过程”小节与 placeholder 条件调整（markdown 函数已导入，无需新增 import）。

### 6. 共享样式 `web/src/assets/.../agent-chat-ui.css`
- 与 index.vue scoped 样式同步补 `.agent-chat-thinking-text`（沿用仓库既有的双份样式维护惯例），预览面板样式一并生效。

### 7. i18n
- `web/i18n/messages/cn.json` / `en.json`（L146 附近）：新增 `chat_thinking_text_label`（中文“思考过程”，英文“Thinking”）。

## 验证
1. 后端：`python -m py_compile` 检查改动文件；按既有隔离实例方式（独立端口 + SQLite 拷贝 + 关限流）启动，用知识库类问题 curl SSE，确认事件序列为 `thinking_move`（或 thinking_text）→ `rag_step`… → `content`，且 DB 中 content 含正式回答、thinking_text 含前导句、content_json 不含前导句。
2. 前端：`pnpm build`（或 dev 起服），用 uitest 账号在「智能体聊天」和「智能体配置预览试聊」各测一轮知识库问答：停顿期间思考面板展开显示前导句 + “思考中…”，正式回答出来后正文正常流式；刷新后重新打开会话，历史回放同样分开展示。
3. 回归：无知识库的普通对话（不触发工具）行为完全不变。

## 已知限制
- 历史存量消息不回迁（旧的会话里这句话仍在正文中）。
- 断线续传发生在思考阶段时，seq 之前的事件照旧丢失（既有限制，本次不扩大战线）。
- 模型真正返回的 `reasoning_content`（推理模型）本次不处理，范围仅限工具调用前导文本。