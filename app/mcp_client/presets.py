"""MCP 预置商店：常用远程 MCP 服务（均为官方托管 streamable_http 端点，2026-08 核实）。

字段说明：
- key：预置唯一标识（前端「一键添加」时带入）
- name / description：展示名称与用途说明
- transport：固定 streamable_http
- url：服务端点
- icon：卡片图标；含冒号为 iconify 图标名（如 mdi:github），否则为图片 URL，缺省时前端用通用图标兜底
- header_fields：需要用户填写的请求头（敏感值加密存储）；空列表表示开箱即用
"""

from __future__ import annotations

MCP_SERVER_PRESETS: list[dict] = [
    {
        "key": "context7",
        "name": "Context7",
        "description": "最新开发文档与代码示例查询（Upstash 托管）。免 Key 可用，配 Key 可提高限额。",
        "transport": "streamable_http",
        "url": "https://mcp.context7.com/mcp",
        "icon": "https://context7.com/favicon.ico",
        "confirm_policy": "never",
        "header_fields": [
            {
                "key": "CONTEXT7_API_KEY",
                "hint": "Context7 API Key（可选；从 context7.com dashboard 获取）",
                "required": False,
            }
        ],
    },
    {
        "key": "tavily",
        "name": "Tavily Search",
        "description": "实时联网搜索与网页内容提取（需 Tavily API Key，免费额度 1000 次/月）。",
        "transport": "streamable_http",
        "url": "https://mcp.tavily.com/mcp/",
        "icon": "https://tavily.com/favicon.ico",
        "confirm_policy": "never",
        "header_fields": [
            {
                "key": "Authorization",
                "hint": "形如 Bearer tvly-xxxx（Tavily 控制台获取）",
                "required": True,
            }
        ],
    },
    {
        "key": "exa",
        "name": "Exa Search",
        "description": "面向 AI 的语义网页搜索与技术文档检索（需 Exa API Key，有免费额度）。",
        "transport": "streamable_http",
        "url": "https://mcp.exa.ai/mcp",
        "icon": "https://exa.ai/favicon.ico",
        "confirm_policy": "never",
        "header_fields": [
            {
                "key": "Authorization",
                "hint": "形如 Bearer <Exa API Key>（exa.ai 控制台获取）",
                "required": True,
            }
        ],
    },
    {
        "key": "github",
        "name": "GitHub",
        "description": "GitHub 仓库/Issue/PR 操作（官方托管，需 Personal Access Token）。",
        "transport": "streamable_http",
        "url": "https://api.githubcopilot.com/mcp",
        "icon": "mdi:github",
        "header_fields": [
            {
                "key": "Authorization",
                "hint": "形如 Bearer <GitHub PAT>（github.com/settings/tokens 创建）",
                "required": True,
            }
        ],
    },
    {
        "key": "huggingface",
        "name": "Hugging Face",
        "description": "模型/数据集/Spaces 检索与 Hub API（需 HF Access Token）。",
        "transport": "streamable_http",
        "url": "https://huggingface.co/mcp",
        "icon": "simple-icons:huggingface",
        "confirm_policy": "never",
        "header_fields": [
            {
                "key": "Authorization",
                "hint": "形如 Bearer hf_xxxx（huggingface.co/settings/tokens 获取）",
                "required": True,
            }
        ],
    },
]


def get_preset_by_key(key: str) -> dict | None:
    for p in MCP_SERVER_PRESETS:
        if p.get("key") == key:
            return p
    return None
