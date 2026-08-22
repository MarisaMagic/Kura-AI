"""编辑器试聊会话识别：前端预览面板使用独立 session，不应影响正式会话的展示。"""

from __future__ import annotations

# 前端 web/src/views/agents/composables/useAgentConfigDiff.js 的 editorPreviewSessionId
# 生成的 session_id 前缀（null agent 为 draft 形态）。
EDITOR_PREVIEW_SESSION_PREFIX = "__editor_preview_"


def is_editor_preview_session(session_id: str | None) -> bool:
    """判断是否为编辑器试聊会话。"""
    return bool(session_id) and str(session_id).startswith(EDITOR_PREVIEW_SESSION_PREFIX)