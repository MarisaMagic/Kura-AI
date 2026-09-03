import uvicorn

from app.settings import settings

if __name__ == "__main__":
    host = (getattr(settings, "UVICORN_HOST", None) or "127.0.0.1").strip() or "127.0.0.1"
    # log_config=None：不用 uvicorn 自带 logging 配置（避免其 dictConfig 覆盖根处理器），
    # 标准库日志经 app.log 的 InterceptHandler 统一汇入 loguru
    uvicorn.run(
        "app:app",
        host=host,
        port=9999,
        reload=bool(settings.DEBUG),
        log_config=None,
    )
