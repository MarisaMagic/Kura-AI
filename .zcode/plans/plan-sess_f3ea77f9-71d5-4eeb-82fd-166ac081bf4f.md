## 目标
把用户默认头像换成 `C:\Users\user\Pictures\Camera Roll\alice.jpg`，图片保存到项目目录内，并让无头像的用户显示该图。

## 现状
- 默认头像定义：`app/utils/avatar.py` 第 7 行 `DEFAULT_AVATAR_URL = "https://avatars.githubusercontent.com/u/54677442?v=4"`（唯一引用处，用于用户无头像时的兜底）
- 用户头像存储目录：`data/user_avatar/`（已存在），由后端 `StaticFiles` 挂载到 `/api/v1/media/user_avatar/<文件名>`（配置在 `app/settings/config.py` 的 `USER_AVATAR_URL_PREFIX`）
- 前端（侧栏用户卡片/个人信息/工作台）直接展示 `userStore.avatar`（后端 userinfo 返回的 URL），无前端兜底
- alice.jpg 已确认是有效 JPEG（2331x2402，~980KB），扩展名 .jpg 在 `ALLOWED_AVATAR_EXTENSIONS` 内

## 改动（2 步）
1. **复制图片**：`C:\Users\user\Pictures\Camera Roll\alice.jpg` → `D:\LLMProjects\Kura-AI\data\user_avatar\alice.jpg`
2. **修改 `app/utils/avatar.py`** 第 7 行：
   - `DEFAULT_AVATAR_URL = f"{settings.USER_AVATAR_URL_PREFIX}/alice.jpg"`（即 `/api/v1/media/user_avatar/alice.jpg`；avatar.py 已 import settings，无新增依赖）

## 验证
- `curl` 访问 `http://localhost:9999/api/v1/media/user_avatar/alice.jpg` 返回 200（StaticFiles 按请求实时读盘，无需重启后端）
- 调 `/base/userinfo` 确认无头像用户的 `avatar` 字段变为新 URL
- 浏览器查看侧栏底部用户卡片头像显示新图

## 注意
- `.gitignore` 第 17 行 `data/` 被忽略，该图片**不会进版本库**；本机运行正常。若希望部署/克隆后默认头像也存在，需要额外处理（如 `git add -f` 强加或放到已提交目录），可以再告诉我。