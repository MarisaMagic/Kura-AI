"""校验本地 data/ 与 MinIO bucket 的对象一致性（数量 + 大小）。

用法：conda run -n Kura-AI python scripts/verify_minio_sync.py
退出码：一致 0；不一致 1。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import object_storage as obs
from app.settings.config import settings

DATA_SUBDIRS = [
    "user_avatar",
    "user_agents_avatar",
    "user_agent_uploads",
    "user_agent_docs",
    "user_agent_images",
]


def main() -> int:
    client = obs.get_client()
    remote = {o.object_name: int(o.size) for o in client.list_objects(settings.S3_BUCKET, recursive=True)}

    data_root = Path(settings.BASE_DIR) / "data"
    local = {}
    for sub in DATA_SUBDIRS:
        base = data_root / sub
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    local[p.relative_to(data_root).as_posix()] = p.stat().st_size

    missing = [k for k in local if k not in remote]
    mismatch = [k for k in local if k in remote and remote[k] != local[k]]
    print(f"local={len(local)} remote={len(remote)} missing={len(missing)} mismatch={len(mismatch)}")
    for k in missing[:10]:
        print("MISSING", k)
    for k in mismatch[:10]:
        print("MISMATCH", k, local[k], remote[k])
    return 0 if not missing and not mismatch else 1


if __name__ == "__main__":
    raise SystemExit(main())
