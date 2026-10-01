"""并发压测脚本（jobs 主路径 / SSE 直连路径），阶梯并发 + 状态监控。

用法示例：
    # 通过 nginx（多副本）压测 jobs 路径
    python tests/load/load_test.py --base http://127.0.0.1:8088/api/v1 --agent-id 1 --levels 25,50,100

    # 直连后端压测 SSE 流
    python tests/load/load_test.py --base http://127.0.0.1:9999/api/v1 --agent-id 1 --mode stream --levels 10,25

指标：端到端时延、错误分布、闸门 inflight/waiting 峰值、loop lag 峰值（/status 采样）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def _load_admin_creds() -> tuple[str, str]:
    user = os.environ.get("LOAD_TEST_USERNAME", "")
    password = os.environ.get("LOAD_TEST_PASSWORD", "")
    if user and password:
        return user, password
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("INITIAL_ADMIN_USERNAME=") and not user:
                user = line.split("=", 1)[1].strip()
            elif line.startswith("INITIAL_ADMIN_PASSWORD=") and not password:
                password = line.split("=", 1)[1].strip()
    return user or "admin", password


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round(p * (len(values) - 1))))
    return round(values[idx], 3)


class Metrics:
    def __init__(self) -> None:
        self.results: list[float] = []
        self.errors: list[str] = []
        self.first_chunk: list[float] = []
        self.status_samples: list[dict] = []
        self.stop = asyncio.Event()

    def summary(self) -> str:
        inflight_peak = max((s.get("inflight", 0) for s in self.status_samples), default=0)
        waiting_peak = max((s.get("waiting", 0) for s in self.status_samples), default=0)
        lag_p99 = max((s["lag_p99"] for s in self.status_samples if s.get("lag_p99") is not None), default=None)
        lag_max = max((s["lag_max"] for s in self.status_samples if s.get("lag_max") is not None), default=None)
        err = Counter(self.errors).most_common(4)
        fc = (
            f" first_chunk P50={pct(self.first_chunk, 0.5)}s P95={pct(self.first_chunk, 0.95)}s"
            if self.first_chunk
            else ""
        )
        return (
            f"ok={len(self.results)} err={len(self.errors)} "
            f"P50={pct(self.results, 0.5)}s P95={pct(self.results, 0.95)}s P99={pct(self.results, 0.99)}s{fc} | "
            f"inflight_peak={inflight_peak} waiting_peak={waiting_peak} "
            f"loop_lag_p99_max={lag_p99}ms lag_max={lag_max}ms"
            + (f" | errors={err}" if err else "")
        )


async def monitor_status(client: httpx.AsyncClient, metrics: Metrics) -> None:
    while not metrics.stop.is_set():
        try:
            d = (await client.get("/base/status", timeout=10)).json()["data"]
            metrics.status_samples.append(
                {
                    "inflight": d["llm_gate"]["inflight"],
                    "waiting": d["llm_gate"]["waiting"],
                    "lag_p99": d["loop_lag"]["p99_ms"],
                    "lag_max": d["loop_lag"]["max_ms"],
                }
            )
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def job_flow(client: httpx.AsyncClient, agent_id: int, vu: int, metrics: Metrics, poll: float) -> None:
    sid = f"load_{vu}_{int(time.time() * 1000)}"
    t0 = time.monotonic()
    try:
        r = await client.post(
            "/user-agent/chat/jobs",
            json={
                "agent_id": agent_id,
                "message": "压测消息",
                "session_id": sid,
                "use_knowledge_retrieval": False,
                "use_web_search": False,
            },
        )
    except Exception as e:
        metrics.errors.append(f"create:{type(e).__name__}")
        return
    if r.status_code != 200:
        metrics.errors.append(f"create_http_{r.status_code}")
        return
    job_id = r.json()["data"]["job_id"]
    deadline = time.monotonic() + 300
    status = None
    while time.monotonic() < deadline:
        try:
            meta = (await client.get(f"/user-agent/chat/jobs/{job_id}")).json().get("data") or {}
            status = meta.get("status")
        except Exception:
            metrics.errors.append("poll_exc")
            return
        if status in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(poll)
    if status == "completed":
        metrics.results.append(time.monotonic() - t0)
    else:
        metrics.errors.append(f"job={status}")


async def stream_flow(client: httpx.AsyncClient, agent_id: int, vu: int, metrics: Metrics) -> None:
    sid = f"loads_{vu}_{int(time.time() * 1000)}"
    t0 = time.monotonic()
    first = None
    try:
        async with client.stream(
            "POST",
            "/user-agent/chat/stream",
            json={
                "agent_id": agent_id,
                "message": "压测消息",
                "session_id": sid,
                "use_knowledge_retrieval": False,
                "use_web_search": False,
            },
            timeout=300,
        ) as resp:
            if resp.status_code != 200:
                metrics.errors.append(f"http_{resp.status_code}")
                return
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    if first is None:
                        first = time.monotonic() - t0
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if ev.get("type") == "error":
                        metrics.errors.append(f"stream_error:{str(ev.get('content'))[:60]}")
    except Exception as e:
        metrics.errors.append(f"stream:{type(e).__name__}")
        return
    if first is not None:
        metrics.first_chunk.append(first)
    metrics.results.append(time.monotonic() - t0)


async def vu_worker(
    client: httpx.AsyncClient,
    agent_id: int,
    vu: int,
    rounds: int,
    metrics: Metrics,
    poll: float,
    mode: str,
) -> None:
    """单个虚拟用户：rounds 轮串行（模拟同一用户连续提问）。"""
    for _ in range(rounds):
        if mode == "stream":
            await stream_flow(client, agent_id, vu, metrics)
        else:
            await job_flow(client, agent_id, vu, metrics, poll)


async def run_level(base: str, agent_id: int, concurrency: int, rounds: int, poll: float, mode: str) -> None:
    limits = httpx.Limits(max_connections=max(64, concurrency * 2), max_keepalive_connections=64)
    async with httpx.AsyncClient(base_url=base, timeout=300, limits=limits) as client:
        user, password = _load_admin_creds()
        r = await client.post("/base/access_token", json={"username": user, "password": password})
        if r.status_code != 200:
            print(f"[FATAL] login failed: {r.status_code} {r.text[:200]}")
            return
        client.headers["token"] = r.json()["data"]["access_token"]

        metrics = Metrics()
        mon = asyncio.create_task(monitor_status(client, metrics))
        t0 = time.monotonic()
        tasks = [
            asyncio.create_task(vu_worker(client, agent_id, vu, rounds, metrics, poll, mode))
            for vu in range(concurrency)
        ]
        await asyncio.gather(*tasks)
        wall = time.monotonic() - t0
        metrics.stop.set()
        await mon
        print(f"[VU={concurrency} rounds={rounds} mode={mode}] wall={wall:.1f}s " + metrics.summary())


async def main() -> None:
    parser = argparse.ArgumentParser(description="Kura-AI load test")
    parser.add_argument("--base", default="http://127.0.0.1:8088/api/v1")
    parser.add_argument("--agent-id", type=int, required=True)
    parser.add_argument("--levels", default="25,50,100")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--poll", type=float, default=0.25, help="jobs 状态轮询间隔（秒）")
    parser.add_argument("--mode", choices=["jobs", "stream"], default="jobs")
    parser.add_argument("--cooldown", type=float, default=3.0)
    args = parser.parse_args()

    for level in [int(x) for x in args.levels.split(",") if x.strip()]:
        print(f"--- concurrency {level} (rounds={args.rounds}) ---")
        await run_level(args.base, args.agent_id, level, args.rounds, args.poll, args.mode)
        await asyncio.sleep(args.cooldown)


if __name__ == "__main__":
    asyncio.run(main())