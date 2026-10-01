import os

import uvicorn

from app.settings import settings

if __name__ == "__main__":
    host = (getattr(settings, "UVICORN_HOST", None) or "127.0.0.1").strip() or "127.0.0.1"
    # 多进程：WEB_CONCURRENCY>1 时每个 uvicorn worker 为独立进程（各自初始化 lifespan）。
    # 生产推荐「每容器 1 进程 + 多容器」由编排/反代扩展；本参数用于单容器内快速多进程。
    try:
        workers = max(1, int(os.environ.get("WEB_CONCURRENCY", "1") or 1))
    except ValueError:
        workers = 1
    # log_config=None：不用 uvicorn 自带 logging 配置（避免其 dictConfig 覆盖根处理器），
    # 标准库日志经 app.log 的 InterceptHandler 统一汇入 loguru
    uvicorn.run(
        "app:app",
        host=host,
        port=9999,
        reload=bool(settings.DEBUG),
        workers=min(workers, 1) if settings.DEBUG else workers,  # reload 与 workers>1 互斥
        timeout_keep_alive=75,
        backlog=1024,
        log_config=None,
    )