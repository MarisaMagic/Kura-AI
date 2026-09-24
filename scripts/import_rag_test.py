"""一键导入 RAG_test 评测数据到实验平台。

每个子集（1doc/2doc/3doc）建为一个数据集：documents/ 下全部 md 走标准入库管线，
dataset.json 的问题（含 gold file_keys）与 ood_questions.json 的库外题导入问题集。

小样本包（tests/RAG_test）与全量包（如 D:\\LLMProjects\\Kura-RAG-eval-full）**必须用不同的
--dataset-prefix**，否则会把全量文档灌进正在跑实验的小样本数据集里。

入库管线按内容哈希去重（unchanged 直接跳过），所以导入中断后重跑即可续传；
--limit 可用于分阶段灌库（先小批量验证，再全量补齐）。

用法（项目根目录）：
    # 小样本（默认）
    python scripts/import_rag_test.py --task all

    # 全量包
    python scripts/import_rag_test.py --task all --root D:\\LLMProjects\\Kura-RAG-eval-full --dataset-prefix RAG_test_full

    # 只灌 1doc 的前 100 篇（分阶段）
    python scripts/import_rag_test.py --task 1doc --root ... --dataset-prefix RAG_test_full --limit 100

    # 文档已灌好，只补问题集
    python scripts/import_rag_test.py --task 1doc --root ... --dataset-prefix RAG_test_full --skip-docs

依赖：PostgreSQL / Milvus / Redis / MinIO 服务在线，.env 配置 EMBEDDING_API_KEY。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.chat.database import init_chat_db  # noqa: E402
from app.experiment import service  # noqa: E402
from app.experiment.service import EXP_KB_USER_ID, exp_agent_id, exp_kb_scope  # noqa: E402
from app.kb.kb_service import run_ingest_pipeline_sync  # noqa: E402

DEFAULT_DATA_ROOT = ROOT / "tests" / "RAG_test"


def ensure_dataset(name: str, description: str) -> tuple[int, bool]:
    """按名称复用或新建数据集，返回 (dataset_id, created)。"""
    existing = next((d for d in service.list_datasets() if d["name"] == name), None)
    if existing:
        return int(existing["id"]), False
    ds = service.create_dataset(name, description, created_by=0)
    return int(ds["id"]), True


def ingest_documents(
    dataset_id: int,
    files: list[Path],
    *,
    retries: int = 1,
) -> tuple[int, int, list[tuple[str, str]]]:
    """
    逐文档走标准入库管线。
    :return: (成功数, 跳过未变化数, [(文件名, 错误信息)])
    """
    scope = exp_kb_scope(dataset_id)
    user_id = EXP_KB_USER_ID
    agent_id = exp_agent_id(dataset_id)
    total = len(files)
    ok = 0
    unchanged = 0
    failures: list[tuple[str, str]] = []
    report_every = max(1, total // 20)

    for i, p in enumerate(files, 1):
        last_err: str | None = None
        for attempt in range(retries + 1):
            try:
                meta = run_ingest_pipeline_sync(
                    kb_scope=scope,
                    user_id=user_id,
                    agent_id=agent_id,
                    display_filename=p.name,
                    source_path=str(p),
                )
                if meta.get("unchanged"):
                    unchanged += 1
                else:
                    ok += 1
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:300]
                if attempt < retries:
                    time.sleep(1.0)
        if last_err is not None:
            failures.append((p.name, last_err))
        if i % report_every == 0 or i == total:
            print(f"[docs] {i}/{total} ok={ok} unchanged={unchanged} failed={len(failures)}", flush=True)

    return ok, unchanged, failures


def import_subset(
    task: str,
    data_root: Path,
    *,
    prefix: str,
    limit: int,
    skip_docs: bool,
    skip_questions: bool,
) -> None:
    subset = data_root / task
    docs_dir = subset / "documents"
    dataset_json = subset / "dataset.json"
    if not docs_dir.is_dir() or not dataset_json.is_file():
        print(f"[skip] {task}: 缺少 documents/ 或 dataset.json")
        return

    name = f"{prefix}-{task}"
    dataset_id, created = ensure_dataset(name, f"{data_root.name} {task} 评测子集（脚本导入）")
    print(f"[{'create' if created else 'reuse'}] 数据集 {name} (id={dataset_id})", flush=True)

    if not skip_docs:
        files = sorted(p for p in docs_dir.glob("*.md"))
        if limit > 0:
            files = files[:limit]
        print(f"[docs] {len(files)} 个文档待入库", flush=True)
        ok, unchanged, failures = ingest_documents(dataset_id, files)
        print(f"[docs] 完成：新增/更新 {ok}，未变化 {unchanged}，失败 {len(failures)}", flush=True)
        for fn, err in failures[:10]:
            print(f"  [fail] {fn}: {err}")
        if len(failures) > 10:
            print(f"  ... 另有 {len(failures) - 10} 个失败（重跑脚本会自动续传）")
        service.refresh_dataset_counts(dataset_id)

    if not skip_questions:
        # dataset.json 始终全量导入（limit 只控制文档，便于后续补齐文档再跑题）
        r1 = service.import_questions(dataset_id, dataset_json.read_bytes(), replace=True)
        print(f"[questions] 库内题导入 {r1['imported']} 条（跳过重复 {r1['skipped']}）", flush=True)
        ood_file = subset / "ood_questions.json"
        if ood_file.is_file():
            r2 = service.import_questions(dataset_id, ood_file.read_bytes(), replace=False)
            print(f"[questions] OOD 题导入 {r2['ood_imported']} 条", flush=True)

        unmatched = service.unmatched_gold_keys(dataset_id)
        if unmatched:
            print(f"[warn] {len(unmatched)} 个 gold 文件名未匹配到已上传文档（若为分阶段导入属正常）")
            print(f"       示例：{unmatched[:5]}")

    service.refresh_dataset_counts(dataset_id)
    ds = service.get_dataset(dataset_id) or {}
    print(f"[done] {name}: 文档 {ds.get('doc_count', 0)}，问题 {ds.get('question_count', 0)}\n", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 RAG_test 评测数据到实验平台")
    parser.add_argument("--task", default="all", choices=["all", "1doc", "2doc", "3doc"])
    parser.add_argument("--root", default=str(DEFAULT_DATA_ROOT), help="评测数据根目录")
    parser.add_argument(
        "--dataset-prefix",
        default="RAG_test",
        help="实验数据集名前缀；全量包务必用不同前缀，避免污染小样本数据集",
    )
    parser.add_argument("--limit", type=int, default=0, help="每个子集最多导入的文档数（0 为全部）")
    parser.add_argument("--skip-docs", action="store_true", help="跳过文档入库，只导问题集")
    parser.add_argument("--skip-questions", action="store_true", help="跳过问题集，只灌文档")
    args = parser.parse_args()

    data_root = Path(args.root)
    if not data_root.is_dir():
        print(f"数据目录不存在: {data_root}")
        sys.exit(1)

    init_chat_db()
    tasks = ["1doc", "2doc", "3doc"] if args.task == "all" else [args.task]
    print(f"数据根目录: {data_root}")
    print(f"数据集前缀: {args.dataset_prefix}-<task>\n")
    for t in tasks:
        import_subset(
            t,
            data_root,
            prefix=args.dataset_prefix,
            limit=args.limit,
            skip_docs=args.skip_docs,
            skip_questions=args.skip_questions,
        )


if __name__ == "__main__":
    main()