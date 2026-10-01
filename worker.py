"""知识库/实验上传 worker 入口：python worker.py（生产由 docker compose 的 kb-worker 服务运行）。"""

import faulthandler
import os
import signal

from app.worker.runner import main

# 诊断能力：SIGUSR1 触发全线程 Python 栈 dump。
# 默认写 stderr；设置 WORKER_FAULT_FILE 时写文件（容器日志通道异常时可 docker exec cat 读取）。
if hasattr(signal, "SIGUSR1"):
    _fault_file = None
    _fault_path = (os.environ.get("WORKER_FAULT_FILE") or "").strip()
    if _fault_path:
        try:
            _fault_file = open(_fault_path, "a", buffering=1)
        except OSError:
            _fault_file = None
    faulthandler.register(signal.SIGUSR1, file=_fault_file, all_threads=True)

if __name__ == "__main__":
    raise SystemExit(main())