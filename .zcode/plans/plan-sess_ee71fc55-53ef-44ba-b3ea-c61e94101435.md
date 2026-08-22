## 修复三处问题:试聊污染 / 冗余路由 / reset_password

### 修复 1:编辑页试聊彻底隔离(不写"最近使用",不从侧栏显示)

试聊使用 session_id `__editor_preview_{agentId}__`(`useAgentConfigDiff.js::editorPreviewSessionId` 生成),后端此前完全无感知,导致:job 完成后无条件 `touch_recent_agent`(chat_job),且消息落库后出现在"最近对话"侧栏与单智能体会话列表(storage)。

改动(消息仍落库,保证试聊多轮上下文正常,只是不展示、不置顶):

1. 新增 `app/chat/preview_session.py`:常量 `EDITOR_PREVIEW_SESSION_PREFIX = "__editor_preview_"` + 判断函数 `is_editor_preview_session(session_id)`。
2. `app/chat/chat_job.py` `_run_chat_job`:job 正常完成处跳过 `touch_recent_agent`(chat_job.py:187-191)。
3. `app/api/v1/user_agents/chat.py`:`chat_stream_endpoint`(207 行)与 `chat_sync_endpoint`(161 行)的 touch 调用加同样的守卫(试聊目前走 jobs 路径,但三处规则一致更稳妥)。
4. `app/chat/storage.py`:`list_session_infos`、`list_session_infos_paginated`、`list_session_infos_all_paginated` 三个查询加 `session_id` 条件过滤 `__editor_preview_%` 前缀;对 Redis 缓存命中结果同样在 Python 侧二次过滤(旧缓存条目立即排除,不等 TTL)。

### 修复 2:删除冗余的多模态知识库路由

现状:`app/api/v1/__init__.py` 把 `multimodal_kb` 子路由挂在 `/media` 前缀下,实际路径变成 `/api/v1/media/media/user_agent_images/...`;图片真实服务走 `app/__init__.py` 的 StaticFiles 挂载(`/api/v1/media/user_agent_images`,指向 `data/user_agent_images`),全代码库无任何调用方引用这条冗余路由(grep 已确认)。

改动:
1. `app/api/v1/__init__.py`:删除 multimodal_kb 的 import(第 10 行)与 `include_router`(第 28 行)。
2. 删除 `app/api/v1/multimodal_kb/` 整个目录包(`__init__.py` + `multimodal_kb.py`,git 可回退)。
3. 保留 `app/__init__.py` 的 StaticFiles 挂载与 `/api/v1/media/` 审计排除路径(静态服务才是真正的图片访问路径)。

### 修复 3:reset_password 改为管理员输入新密码 + 审计日志排除

现状缺陷:后端 `generate_password()` 随机生成 12 位密码并在 `msg` 明文返回;审计中间件会把响应体原样写入 `audit_log` 表(明文密码落库);而前端 `system/user/index.vue` 把提示写死成"重置为123456"(与真实密码不符,且 123456 会被强度校验拒绝)——管理员实际拿不到新密码。

改动(采用"管理员输入新密码"方案):
1. `app/controllers/user.py::reset_password(user_id, new_password)`:保留超管 403 拦截;`get_password_hash(new_password)` 已内含强度校验(≥8 位、字母+数字混合),捕获 ValueError 转 HTTPException 400 返回具体原因;保存后不再返回密码。
2. `app/api/v1/users/users.py::reset_password`:`user_id` + `new_password` 两个 Body 参数(JSON body `{"user_id":..,"new_password":..}`),返回 `Success(msg="密码已重置")`,响应中不再含任何明文密码。
3. `app/core/init_app.py` 审计 `exclude_paths` 追加 `/api/v1/user/reset_password`(请求体里的新密码不写入 AuditLog;与已排除的登录/注册保持一致)。
4. `web/src/views/system/user/index.vue`:把写死"123456"的 NPopconfirm 换成 NModal 弹窗,管理员输入新密码(带强度提示 ≥8 位、字母+数字),确认后 `api.resetPassword({ user_id, new_password })`,成功提示"密码已重置"并刷新列表,400 错误展示后端具体原因;`vPermission` 指令和超管行隐藏逻辑保持不变。

### 验证方式

- 后端无测试基建,采用人工冒烟:启动 uvicorn 后 ① 造一个 `__editor_preview_x__` 会话,确认 `/chat/sessions`、`/chat/sessions/all` 不返回它;② 试聊完成后再查 `/recent_agents`,确认未被 touch;③ 调 `/user/reset_password`,确认响应无密码、`audit_log` 表无该条记录、`/api/v1/media/...` 图片仍可访问(StaticFiles)。
- 前端:`pnpm dev` 下走一遍用户管理"重置密码"弹窗、编辑页试聊、侧栏"最近对话"。
- 注意:需要 `docker compose up -d`(PostgreSQL/Redis)才能跑后端;若本地未起,先完成代码改动并做语法检查。

### 涉及文件

后端:新增 `app/chat/preview_session.py`;修改 `app/chat/chat_job.py`、`app/api/v1/user_agents/chat.py`、`app/chat/storage.py`、`app/api/v1/__init__.py`、`app/controllers/user.py`、`app/api/v1/users/users.py`、`app/core/init_app.py`;删除 `app/api/v1/multimodal_kb/`。
前端:修改 `web/src/views/system/user/index.vue`。