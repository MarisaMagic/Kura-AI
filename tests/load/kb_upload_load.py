"""多用户知识库文档上传压测（经 Nginx，生产多副本）。

用法：
    # 管理员上传一份短文档，直到 completed / failed
    python tests/load/kb_upload_load.py --mode smoke

    # 20 个用户各传 3 份约 1KB Markdown，同时提交
    python tests/load/kb_upload_load.py --mode load --users 20 --files 3

账号口令默认读 .env 的 INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD。
压测用户与文档留在库里，便于对照 worker 日志。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"completed", "failed", "timeout", "cancelled"}
USER_PASSWORD = "LoadKb2026a"


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
                user = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("INITIAL_ADMIN_PASSWORD=") and not password:
                password = line.split("=", 1)[1].strip().strip('"').strip("'")
    return user or "admin", password


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round(p * (len(values) - 1))))
    return round(values[idx], 3)


def _doc_bytes(user_index: int, file_index: int) -> bytes:
    body = (
        f"# 知识库并发压测 {user_index}-{file_index}\n\n"
        + "这是一段用于解析与向量化的短文本。队列只传递任务号，文件在对象存储。\n" * 12
    )
    raw = body.encode("utf-8")
    if len(raw) < 900:
        raw = raw + ("补充段落。\n".encode("utf-8") * 20)
    return raw[:1200]


class DepthSampler:
    def __init__(self) -> None:
        self.samples: list[int] = []
        self.stop = asyncio.Event()

    async def run(self, client: httpx.AsyncClient) -> None:
        while not self.stop.is_set():
            try:
                data = (await client.get("/base/status", timeout=10)).json().get("data") or {}
                kb = data.get("kb_queue") or {}
                if "depth" in kb:
                    self.samples.append(int(kb["depth"]))
            except Exception:
                pass
            await asyncio.sleep(1.0)


async def login(client: httpx.AsyncClient, username: str, password: str) -> str:
    # 登录按来源 IP 每分钟 20 次。多用户同时登录会撞上这个上限，429 时等待后重试。
    last = ""
    for attempt in range(8):
        r = await client.post("/base/access_token", json={"username": username, "password": password})
        if r.status_code == 429:
            last = r.text[:200]
            await asyncio.sleep(8 + attempt * 2)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"login {username} failed: {r.status_code} {r.text[:300]}")
        token = (r.json().get("data") or {}).get("access_token")
        if not token:
            raise RuntimeError(f"login {username} missing token: {r.text[:300]}")
        return token
    raise RuntimeError(f"login {username} rate limited: {last}")


def _authed(base: str, token: str, limits: httpx.Limits | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base,
        timeout=120,
        headers={"token": token},
        limits=limits or httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )


async def _role_id(client: httpx.AsyncClient) -> int:
    r = await client.get("/role/list", params={"page": 1, "page_size": 50, "role_name": "普通用户"})
    if r.status_code != 200:
        raise RuntimeError(f"role list failed: {r.status_code} {r.text[:300]}")
    rows = r.json().get("data") or []
    for row in rows:
        if row.get("name") == "普通用户":
            return int(row["id"])
    raise RuntimeError("未找到「普通用户」角色")


async def _ensure_user(admin: httpx.AsyncClient, role_id: int, username: str, email: str) -> None:
    r = await admin.post(
        "/user/create",
        json={
            "email": email,
            "username": username,
            "password": USER_PASSWORD,
            "is_active": True,
            "role_ids": [role_id],
            "dept_id": 0,
        },
    )
    if r.status_code == 200:
        return
    text = r.text
    if r.status_code == 400 and "already exists" in text:
        return
    raise RuntimeError(f"create user {username} failed: {r.status_code} {text[:300]}")


async def _ensure_agent(client: httpx.AsyncClient, name: str) -> int:
    listed = await client.get("/user-agent/list", params={"page": 1, "page_size": 20})
    if listed.status_code != 200:
        raise RuntimeError(f"agent list failed: {listed.status_code} {listed.text[:300]}")
    for row in listed.json().get("data") or []:
        if row.get("name") == name and row.get("id"):
            return int(row["id"])
    created = await client.post(
        "/user-agent/create",
        json={
            "name": name,
            "model_name": "qwen-plus",
            "api_key": "load-test-not-used",
            "description": "kb concurrency load",
        },
    )
    if created.status_code != 200:
        raise RuntimeError(f"create agent failed: {created.status_code} {created.text[:300]}")
    agent_id = (created.json().get("data") or {}).get("id")
    if not agent_id:
        raise RuntimeError(f"create agent missing id: {created.text[:300]}")
    return int(agent_id)


async def _upload_one(
    client: httpx.AsyncClient, agent_id: int, filename: str, content: bytes
) -> tuple[str | None, float, str | None]:
    t0 = time.monotonic()
    try:
        r = await client.post(
            "/user-agent/kb/upload",
            params={"agent_id": agent_id},
            files={"file": (filename, content, "text/markdown")},
        )
    except Exception as e:
        return None, time.monotonic() - t0, f"exc:{type(e).__name__}"
    elapsed = time.monotonic() - t0
    if r.status_code != 200:
        return None, elapsed, f"http_{r.status_code}:{(r.text or '')[:120]}"
    task_id = (r.json().get("data") or {}).get("task_id")
    if not task_id:
        return None, elapsed, "missing_task_id"
    return str(task_id), elapsed, None


async def _poll_owned(
    client: httpx.AsyncClient, task_ids: list[str], deadline: float
) -> dict[str, dict]:
    pending = set(task_ids)
    found: dict[str, dict] = {}
    while pending and time.monotonic() < deadline:
        ids = ",".join(pending)
        try:
            r = await client.get("/user-agent/kb/upload/status/batch", params={"task_ids": ids})
        except Exception:
            await asyncio.sleep(2)
            continue
        if r.status_code != 200:
            await asyncio.sleep(2)
            continue
        items = (r.json().get("data") or {}).get("items") or {}
        for tid, meta in items.items():
            if not isinstance(meta, dict):
                continue
            status = str(meta.get("status") or "")
            if status in TERMINAL:
                found[tid] = meta
                pending.discard(tid)
        if pending:
            await asyncio.sleep(2)
    return found


async def smoke(base: str) -> int:
    user, password = _load_admin_creds()
    async with httpx.AsyncClient(base_url=base, timeout=60) as anon:
        health = await anon.get("/base/health")
        status = await anon.get("/base/status")
        print(f"[smoke] health={health.status_code} status={status.status_code}")
        if health.status_code != 200 or status.status_code != 200:
            print(health.text[:200], status.text[:200])
            return 1
        kb = (status.json().get("data") or {}).get("kb_queue")
        print(f"[smoke] kb_queue={kb}")
        token = await login(anon, user, password)
    async with _authed(base, token) as client:
        agent_id = await _ensure_agent(client, "kb-smoke")
        task_id, elapsed, err = await _upload_one(
            client, agent_id, "kb-smoke.md", _doc_bytes(0, 0)
        )
        print(f"[smoke] upload agent={agent_id} accept={elapsed:.3f}s task={task_id} err={err}")
        if not task_id:
            return 1
        metas = await _poll_owned(client, [task_id], time.monotonic() + 960)
        meta = metas.get(task_id) or {}
        print(f"[smoke] terminal status={meta.get('status')} error_type={meta.get('error_type')} error={meta.get('error')}")
        return 0 if meta.get("status") == "completed" else 1


async def load(base: str, users: int, files: int) -> int:
    admin_user, admin_password = _load_admin_creds()
    limits = httpx.Limits(max_connections=max(64, users * files), max_keepalive_connections=64)
    async with httpx.AsyncClient(base_url=base, timeout=120, limits=limits) as anon:
        token = await login(anon, admin_user, admin_password)
    async with _authed(base, token) as admin:
        role_id = await _role_id(admin)
        for i in range(1, users + 1):
            await _ensure_user(admin, role_id, f"kload{i:02d}", f"kload{i:02d}@example.com")
        sampler = DepthSampler()
        mon = asyncio.create_task(sampler.run(admin))

        login_slots = asyncio.Semaphore(4)

        async def prepare(i: int) -> tuple[httpx.AsyncClient, int, str]:
            async with login_slots:
                async with httpx.AsyncClient(base_url=base, timeout=60) as anon:
                    user_token = await login(anon, f"kload{i:02d}", USER_PASSWORD)
            client = _authed(base, user_token, limits)
            agent_id = await _ensure_agent(client, "kb-load")
            return client, agent_id, f"kload{i:02d}"

        prepared = await asyncio.gather(*(prepare(i) for i in range(1, users + 1)))
        clients = [p[0] for p in prepared]

        async def one(client: httpx.AsyncClient, agent_id: int, username: str, n: int) -> dict:
            filename = f"kb-load-{username}-{n}.md"
            task_id, elapsed, err = await _upload_one(client, agent_id, filename, _doc_bytes(int(username[-2:]), n))
            return {
                "username": username,
                "client": client,
                "task_id": task_id,
                "accept_s": elapsed,
                "error": err,
            }

        jobs = [
            one(client, agent_id, username, n)
            for client, agent_id, username in prepared
            for n in range(files)
        ]
        t0 = time.monotonic()
        accepted = await asyncio.gather(*jobs)
        accept_wall = time.monotonic() - t0

        by_user: dict[str, list[dict]] = {}
        for row in accepted:
            by_user.setdefault(row["username"], []).append(row)

        async def wait_user(username: str, rows: list[dict]) -> list[dict]:
            client = rows[0]["client"]
            ids = [r["task_id"] for r in rows if r["task_id"]]
            metas = await _poll_owned(client, ids, time.monotonic() + 960) if ids else {}
            for row in rows:
                meta = metas.get(row["task_id"] or "") or {}
                row["status"] = meta.get("status")
                row["error_type"] = meta.get("error_type")
                row["task_error"] = meta.get("error")
                if row["task_id"] and row["status"] in TERMINAL:
                    row["done_s"] = time.monotonic() - t0
            return rows

        finished_groups = await asyncio.gather(*(wait_user(u, rows) for u, rows in by_user.items()))
        wall = time.monotonic() - t0
        sampler.stop.set()
        await mon
        for client in clients:
            await client.aclose()

    rows = [row for group in finished_groups for row in group]
    accept_ok = [r["accept_s"] for r in rows if r["task_id"]]
    accept_err = [r["error"] for r in rows if r["error"]]
    done = [r["done_s"] for r in rows if r.get("done_s") is not None]
    statuses = Counter(str(r.get("status") or "missing") for r in rows if r["task_id"])
    throttled = [
        r for r in rows if r.get("status") == "failed" and r.get("error_type") == "throttled"
    ]
    other_failed = [
        r
        for r in rows
        if r.get("status") == "failed" and r.get("error_type") != "throttled"
    ]
    stuck = [r["task_id"] for r in rows if r["task_id"] and r.get("status") not in TERMINAL]
    http5xx = [e for e in accept_err if e and "http_5" in e]
    depth_peak = max(sampler.samples) if sampler.samples else None
    depth_last = sampler.samples[-1] if sampler.samples else None
    print(
        f"[load] users={users} files={files} accept_wall={accept_wall:.1f}s wall={wall:.1f}s "
        f"accepted={len(accept_ok)} accept_err={len(accept_err)} "
        f"accept_P50={pct(accept_ok, 0.5)}s accept_P95={pct(accept_ok, 0.95)}s "
        f"done_P50={pct(done, 0.5)}s done_P95={pct(done, 0.95)}s"
    )
    print(f"[load] statuses={dict(statuses)} throttled={len(throttled)} other_failed={len(other_failed)}")
    print(f"[load] depth_peak={depth_peak} depth_last={depth_last} stuck={stuck[:10]}")
    if accept_err:
        print(f"[load] accept_errors={Counter(accept_err).most_common(5)}")
    if other_failed:
        sample = other_failed[0]
        print(f"[load] other_failed_sample={sample.get('task_error')}")
    if http5xx or stuck:
        return 1
    if any(r["task_id"] and r.get("status") not in TERMINAL for r in rows):
        return 1
    return 0


async def main() -> None:
    parser = argparse.ArgumentParser(description="KB upload concurrency load")
    parser.add_argument("--base", default="http://127.0.0.1:8088/api/v1")
    parser.add_argument("--mode", choices=["smoke", "load"], default="load")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--files", type=int, default=3)
    args = parser.parse_args()
    if args.mode == "smoke":
        code = await smoke(args.base)
    else:
        code = await load(args.base, args.users, args.files)
    raise SystemExit(code)


if __name__ == "__main__":
    asyncio.run(main())
