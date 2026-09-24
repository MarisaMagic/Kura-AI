"""知识库/实验上传 worker 入口：python worker.py（生产由 docker compose 的 kb-worker 服务运行）。"""

from app.worker.runner import main

if __name__ == "__main__":
    raise SystemExit(main())