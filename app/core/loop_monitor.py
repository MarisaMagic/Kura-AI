"""
事件循环延迟监控（阶段 1 验收核心指标）。

后台任务以固定间隔采样 loop lag（实际睡眠时长 - 期望时长），滑动窗口内
统计 P50/P99/最大值；供 ``/api/v1/base/status`` 暴露与压测验收
（目标：100 并发下 P99 < 50ms）。

lag 显著升高说明事件循环被同步调用阻塞，应定位 ``run_sync`` 未覆盖的慢调用。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Deque, Optional

from app.log import logger


class LoopLagMonitor:
    """事件循环延迟采样器（单例由模块级函数管理）。"""

    def __init__(self, interval: float = 0.1, window: int = 600) -> None:
        self.interval = max(0.02, float(interval))
        self.window = max(60, int(window))
        self._samples: Deque[float] = deque(maxlen=self.window)
        self._task: Optional[asyncio.Task] = None
        self._lock = threading.Lock()

    async def _run(self) -> None:
        while True:
            start = time.perf_counter()
            await asyncio.sleep(self.interval)
            lag = max(0.0, time.perf_counter() - start - self.interval)
            with self._lock:
                self._samples.append(lag)

    def start(self) -> None:
        """在事件循环内启动采样任务（幂等）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._run(), name="loop-lag-monitor")
        logger.info("事件循环延迟监控已启动（间隔 {:.0f}ms）", self.interval * 1000)

    def stop(self) -> None:
        """停止采样任务。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    def stats(self) -> dict:
        """滑窗统计（毫秒）。样本不足时返回 None 百分位。"""
        with self._lock:
            samples = sorted(self._samples)
        if not samples:
            return {"samples": 0, "p50_ms": None, "p99_ms": None, "max_ms": None}

        def _pct(p: float) -> float:
            idx = min(len(samples) - 1, int(round(p * (len(samples) - 1))))
            return round(samples[idx] * 1000, 1)

        return {
            "samples": len(samples),
            "p50_ms": _pct(0.50),
            "p99_ms": _pct(0.99),
            "max_ms": round(samples[-1] * 1000, 1),
        }


_monitor: Optional[LoopLagMonitor] = None


def start_loop_monitor(interval: float = 0.1) -> None:
    """启动全局监控（lifespan 调用）。"""
    global _monitor
    if _monitor is None:
        _monitor = LoopLagMonitor(interval=interval)
    _monitor.start()


def stop_loop_monitor() -> None:
    global _monitor
    if _monitor is not None:
        _monitor.stop()


def get_loop_lag_stats() -> dict:
    if _monitor is None:
        return {"samples": 0, "p50_ms": None, "p99_ms": None, "max_ms": None}
    return _monitor.stats()
