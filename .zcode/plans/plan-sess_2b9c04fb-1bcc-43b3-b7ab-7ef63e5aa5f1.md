# 修复：上传报「无法获取处理进度」但文档其实已入库

## 诊断结论（已证实）

该报错是前端 `startPolling` 的兜底文案，触发条件唯一：上传 POST 成功拿到 task_id 后，`GET /kb/upload/status` **连续失败 4 次**。它不是「文档处理失败」。

现场证据 + 你的确认：库里2011-2012赛季报告.pdf 的 Redis 任务记录是 `completed`（文件 13:55 已落盘、已出现在列表）——所以这是一次**进度跟踪层故障**，处理层完全正常。促成这次误报的机制有四个：

1. **9999 双实例抢端口**：两个 `python run.py` 同时监听 0.0.0.0:9999（172788=我上次会话遗留、174368=你自己起的）。你在 13:54:51 启动自己实例的窗口期恰好压在第一份文档的轮询上，连接抖动导致轮询连环失败。
2. **轮询层把瞬时抖动当终局**：任何一种失败（404/超时/网络错）混在一起计数，4 连败就宣告任务失效并停轮询，而后端照常跑完。文案还误导用户以为文档处理失败。
3. **Redis 失败全静默**：`cache.py` 的 get/set 全部 `except: return`，不写任何日志——meta 写入失败时接口照样返回 200+task_id（幽灵任务），前后端零痕迹，正合「各处均无报错」。
4. **真 bug（我上轮引入）**：`kb_job._update_meta` 在 `get_json` 读到 None 时会用 `{"task_id"}` 重建快照，**丢掉 user_id 等身份字段** → 状态接口此后对该任务**永久 404**。

## 修复内容

### 后端
1. **`app/kb/kb_job.py`**
   - `_update_meta` 增加 `identity` 参数（task_id/user_id/agent_id/kb_scope/display_filename）；`get_json` 失败重建快照时用 identity 完整恢复，并用 loguru 记 warning。根治「永久 404」bug。
   - `create_kb_upload_job` 初始 meta 写入做 2 次重试；仍失败 → 记 ERROR 日志并返回 `None`，**不再**创建幽灵任务。
2. **`app/api/v1/user_agents/kb.py`**：`create_kb_upload_job` 返回 None 时返回 `Fail(code=503, msg="任务状态初始化失败（Redis 暂不可用），请稍后重试")`——用户立刻得到真实原因，而不是 3.5 秒后的误导文案。
3. **`app/chat/cache.py`**：加 loguru 日志，`get_json/set_json/set_nx/delete` 的 except 分支各记一行 warning（带异常文本），消灭「前端后端日志全空」的诊断死角。

### 前端
4. **`web/src/views/agents/knowledge-base/index.vue`** 轮询 catch 按错误类型分流：
   - **HTTP 404**（任务确定不存在）→ 立即终局（不等 4 连败），显示新文案 kb_upload_gone「上传任务不存在或已过期——后台可能仍在处理，请查看下方文件列表确认文档是否已入库」；`finishTask` 会自动 `fetchList()` 刷新列表，用户能立刻看到文档其实已入库。
   - **401/403** → 静默停轮询（交给拦截器的登出流程），不误报「处理失败」。
   - **网络错/超时/其他** → 维持重试，但第 2 次连续失败时就额外 `fetchList()` 一次兜底对账；最终放弃文案改为「进度跟踪中断（网络或服务异常）——文档可能已处理完成，请查看文件列表」。
5. **`web/src/api/index.js`**：`uploadKbDocument` 增加可选 config 透传；`onUploadChange` 传 `noErrorMessage: true`，修复上传失败时全局+局部双弹窗问题。
6. **`web/i18n/messages/cn.json` + `en.json`**：新增 kb_upload_gone；kb_upload_status_lost 文案补上「请查看文件列表确认」指引。

### 环境治理
7. 先核实命令行归属后 `taskkill //F //T //PID 172788`（我遗留的实例），保留你的 174368；确认 9999 仅剩单一监听。
8. 清理 Redis 中我自检遗留的测试键（`kura_ai:kb_upload_job:*` 且 meta.user_id=977，现剩 1 个）。

## 验证
9. 全部改动文件 `py_compile`；`npm run build` 通过。
10. 隔离实例（9998 + DB 拷贝 + 文档目录重定向）冒烟两条关键路径：
    - 正常上传 → 状态轮询到 completed（回归确认轮询链路没改坏）；
    - 用 `REDIS_URL=redis://127.0.0.1:6399/0`（死端口）起实例 → 上传必须返回 **503 + 后端日志 ERROR**（此前是静默 200 幽灵任务）；
    - 临时脚本模拟 `get_json` 返回 None → 确认 identity 重建后 user_id 不再丢失。
11. 杀掉遗留进程后用 curl 验证 9999 单实例健康（registration_enabled 200 + status 接口 401/422 行为正常）。
12. 你的后端是 reload=True，改完 .py 自动生效；前端若是 dev 模式自动热更，若跑的构建产物需重新 `npm run build`。

不做的事：不动上传/处理管线本身（已证明工作正常）；不提交 git。