"""将本地 data/ 目录的存量用户文件迁移到 MinIO 对象存储。

对象存储改造（统一 MinIO）前的存量数据搬迁工具。

用法（项目根目录，conda 环境 Kura-AI，minio-app 容器已启动）：
    conda run -n Kura-AI python scripts/migrate_to_minio.py --dry-run   # 只统计与预览，不上传
    conda run -n Kura-AI python scripts/migrate_to_minio.py             # 执行迁移（幂等，可重跑）

key 规则：data/<prefix>/<rest> -> <prefix>/<rest>（统一正斜杠），与运行期
配置项 USER_*_ROOT 前缀约定一致；数据库中的 stored_relpath 等字段无需变更。
幂等：已存在且大小一致的对象跳过；上传后逐对象校验大小。
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path

# 允许从项目根目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger
from minio.error import S3Error

from app.core import object_storage as obs
from app.settings.config import settings

# data/ 下参与迁移的子目录（与配置项 USER_*_ROOT 前缀一一对应）
DATA_SUBDIRS = [
    "user_avatar",
    "user_agents_avatar",
    "user_agent_uploads",
    "user_agent_docs",
    "user_agent_images",
]


def iter_local_files(data_root: Path):
    """产出 (本地路径, 对象 key)；key 为 data/ 下的相对路径（正斜杠）。"""
    for sub in DATA_SUBDIRS:
        base = data_root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                yield p, p.relative_to(data_root).as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移本地 data/ 用户文件到 MinIO 对象存储")
    parser.add_argument("--dry-run", action="store_true", help="只统计与预览，不上传")
    parser.add_argument(
        "--data-root",
        default=str(Path(settings.BASE_DIR) / "data"),
        help="本地数据目录（默认 <项目根>/data）",
    )
    args = parser.parse_args()

    data_root = Path(args.data_root)
    if not data_root.is_dir():
        logger.warning(f"本地数据目录不存在，无存量可迁移: {data_root}")
        return 0

    client = obs.get_client()
    if args.dry_run:
        total = 0
        total_bytes = 0
        for path, key in iter_local_files(data_root):
            size = path.stat().st_size
            total += 1
            total_bytes += size
            print(f"[DRY] {path} -> s3://{settings.S3_BUCKET}/{key} ({size} B)")
        print(f"[DRY] 共 {total} 个文件，{total_bytes} 字节")
        return 0

    obs.ensure_bucket()

    uploaded = 0
    skipped = 0
    failed = 0
    failures: list[str] = []
    for path, key in iter_local_files(data_root):
        size = path.stat().st_size
        try:
            try:
                st = client.stat_object(settings.S3_BUCKET, key)
                if int(st.size) == size:
                    skipped += 1
                    continue
            except S3Error:
                pass  # 对象不存在则上传
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            client.fput_object(settings.S3_BUCKET, key, str(path), content_type=mime)
            st = client.stat_object(settings.S3_BUCKET, key)
            if int(st.size) != size:
                raise RuntimeError(f"上传后大小不一致 local={size} remote={st.size}")
            uploaded += 1
            logger.info(f"已迁移 {key} ({size} B)")
        except Exception as e:
            failed += 1
            failures.append(f"{key}: {e}")
            logger.error(f"迁移失败 {key}: {e}")

    print(f"迁移完成：成功 {uploaded}，跳过（已存在且一致） {skipped}，失败 {failed}")
    if failures:
        print("失败清单：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("可人工抽查 MinIO 控制台（默认 http://127.0.0.1:9003）确认后，再清理本地 data/ 目录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
