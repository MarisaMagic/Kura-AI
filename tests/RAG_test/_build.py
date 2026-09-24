"""从 CRUD-RAG 抽取 Kura-AI 闭集 RAG 评测包（1doc / 2doc / 3doc）。

默认输出到脚本同级目录（tests/RAG_test/<task>/），抽取 80 题 + 10 OOD。
可用 --n-cases 0 抽全量合格样本、--ood-count 扩大留出集、--out-root 输出到仓库外。

留出集（held-out）语义：ood_count 条样本先被划出且**不写入 documents/**，
题目进 ood_questions.json，用于测「知识库中未找到相关资料」的拒答。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SEED = 42
DEFAULT_N_CASES = 80
DEFAULT_OOD_COUNT = 10
MIN_NEWS_LEN = 200

# 80 题基准配额；--n-cases 变化时按比例缩放
STRATUM_QUOTA = {
    "multi": 15,
    "numeric": 40,
    "named": 25,
}
QUOTA_BASE = sum(STRATUM_QUOTA.values())

TASK_NEWS_KEYS = {
    "1doc": ("news1",),
    "2doc": ("news1", "news2"),
    "3doc": ("news1", "news2", "news3"),
}

TASK_SOURCE_KEY = {
    "1doc": "questanswer_1doc",
    "2doc": "questanswer_2docs",
    "3doc": "questanswer_3docs",
}

DEFAULT_SOURCE = Path(r"D:\LLMProjects\CRUD_RAG\data\crud_split\split_merged.json")
ROOT = Path(__file__).resolve().parent

_MULTI_RE = re.compile(r"同时|另外[，,]|并且请|以及.{0,12}[？?]|[？?].+[？?]")
_NUMERIC_RE = re.compile(r"\d")
_NAMED_RE = re.compile(
    r"《[^》]+》|委员会|管理局|卫健委|药监局|国务院|人民政府|有限公司|"
    r"大学|医院|研究院|新华社|央行|工信部|应急管理部"
)
_SPLIT_ANSWER_RE = re.compile(r"[，。；、：:？?！!（）()\u201c\u201d\"\s]+")
_SKIP_ANSWER_PARTS = frozenset(
    {
        "因此",
        "此外",
        "同时",
        "以及",
        "包括",
        "表示",
        "指出",
        "根据",
        "其中",
        "分别",
        "进行",
        "相关",
        "上述",
    }
)
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def clean_news(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def news_keys(task: str) -> tuple[str, ...]:
    return TASK_NEWS_KEYS[task]


def concat_news(item: dict, task: str) -> str:
    return "\n".join(clean_news(item.get(k) or "") for k in news_keys(task))


def answer_supported(answer: str, corpus: str) -> bool:
    parts = [p.strip() for p in _SPLIT_ANSWER_RE.split(answer) if len(p.strip()) >= 2]
    parts = [p for p in parts if p not in _SKIP_ANSWER_PARTS]
    if not parts:
        return any(token in corpus for token in re.findall(r"[\u4e00-\u9fff]{2,}", answer)[:8])
    return any(p in corpus for p in parts)


def classify(item: dict) -> str:
    q = item["questions"]
    a = item["answers"]
    blob = f"{q}\n{a}"
    if _MULTI_RE.search(q):
        return "multi"
    if _NUMERIC_RE.search(blob):
        return "numeric"
    if _NAMED_RE.search(blob):
        return "named"
    return "other"


def is_eligible(item: dict, task: str, *, apply_filter: bool = True) -> bool:
    question = (item.get("questions") or "").strip()
    answer = (item.get("answers") or "").strip()
    if not question or not answer or not (item.get("ID") or "").strip():
        return False
    if not apply_filter:
        # 仅要求待用新闻存在且非空
        return all((item.get(k) or "").strip() for k in news_keys(task))
    for key in news_keys(task):
        if len(clean_news(item.get(key) or "")) < MIN_NEWS_LEN:
            return False
    return answer_supported(answer, concat_news(item, task))


def take_stratum(pool: list[dict], n: int, used: set[str], rng: random.Random) -> list[dict]:
    candidates = [x for x in pool if x["ID"] not in used]
    rng.shuffle(candidates)
    picked = candidates[:n]
    used.update(x["ID"] for x in picked)
    return picked


def scaled_quota(n_cases: int) -> dict[str, int]:
    """把 80 题基准配额按比例缩放到 n_cases，余数补给 numeric。"""
    quota = {k: int(round(n_cases * v / QUOTA_BASE)) for k, v in STRATUM_QUOTA.items()}
    diff = n_cases - sum(quota.values())
    quota["numeric"] = max(0, quota["numeric"] + diff)
    return quota


def stratified_sample(eligible: list[dict], rng: random.Random, n_cases: int) -> list[dict]:
    if n_cases >= len(eligible):
        picked = list(eligible)
        rng.shuffle(picked)
        return picked

    by_stratum: dict[str, list[dict]] = defaultdict(list)
    for item in eligible:
        by_stratum[classify(item)].append(item)

    quota = scaled_quota(n_cases)
    used: set[str] = set()
    selected: list[dict] = []
    for name in STRATUM_QUOTA:  # 保持与既有 80 题抽取一致的顺序
        selected.extend(take_stratum(by_stratum.get(name, []), quota.get(name, 0), used, rng))

    if len(selected) < n_cases:
        leftover = [x for x in eligible if x["ID"] not in used]
        rng.shuffle(leftover)
        extra = leftover[: n_cases - len(selected)]
        selected.extend(extra)
        used.update(x["ID"] for x in extra)

    rng.shuffle(selected)
    return selected[:n_cases]


def file_stem(item_id: str, news_key: str, task: str) -> str:
    if task == "1doc":
        return f"{item_id}.md"
    return f"{item_id}_{news_key}.md"


def to_case(item: dict, task: str) -> dict:
    keys = news_keys(task)
    file_names = [file_stem(item["ID"], k, task) for k in keys]
    case = {
        "id": item["ID"],
        "event": (item.get("event") or "").strip(),
        "question": item["questions"].strip(),
        "answer": item["answers"].strip(),
        "stratum": classify(item),
    }
    if task == "1doc":
        case["document"] = f"documents/{file_names[0]}"
        case["file_key"] = file_names[0]
    else:
        case["documents"] = [f"documents/{name}" for name in file_names]
        case["file_keys"] = file_names
    return case


def write_documents(docs_dir: Path, item: dict, task: str) -> None:
    event = (item.get("event") or "").strip() or item["ID"]
    for key in news_keys(task):
        body = clean_news(item.get(key) or "")
        title = event if task == "1doc" else f"{event}（{key}）"
        path = docs_dir / file_stem(item["ID"], key, task)
        path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")


def build_task(
    task: str,
    source: Path,
    raw: dict,
    out_root: Path,
    *,
    n_cases: int,
    ood_count: int,
    apply_filter: bool,
) -> dict:
    source_key = TASK_SOURCE_KEY[task]
    items = raw.get(source_key) or []
    if not items:
        raise SystemExit(f"{source_key} is empty")

    eligible = [x for x in items if is_eligible(x, task, apply_filter=apply_filter)]
    if not eligible:
        raise SystemExit(f"{task}: no eligible samples")
    if ood_count >= len(eligible):
        raise SystemExit(f"{task}: ood_count {ood_count} >= eligible {len(eligible)}")

    rng = random.Random(SEED)

    take_all = n_cases <= 0 or n_cases >= len(eligible) - ood_count
    if take_all:
        # 先划出留出集，其余全部作为库内题
        shuffled = list(eligible)
        rng.shuffle(shuffled)
        ood_items = shuffled[:ood_count]
        selected = shuffled[ood_count:]
        if n_cases > 0 and n_cases < len(eligible) - ood_count:
            print(f"[warn] {task}: n_cases {n_cases} 超过可用池，已取全部 {len(selected)} 题")
    else:
        selected = stratified_sample(eligible, rng, n_cases)
        selected_ids = {x["ID"] for x in selected}
        unused = [x for x in eligible if x["ID"] not in selected_ids]
        rng.shuffle(unused)
        ood_items = unused[:ood_count]

    if len(ood_items) < ood_count:
        raise SystemExit(f"{task}: not enough held-out samples: {len(ood_items)} < {ood_count}")

    out_dir = out_root / task
    docs_dir = out_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for old in docs_dir.glob("*.md"):
        old.unlink()
    for item in selected:
        write_documents(docs_dir, item, task)

    cases = [to_case(x, task) for x in selected]
    strata_counts: dict[str, int] = defaultdict(int)
    for c in cases:
        strata_counts[c["stratum"]] += 1

    dataset = {
        "version": "1.0",
        "source": f"CRUD-RAG {source_key}",
        "source_file": str(source),
        "task": task,
        "seed": SEED,
        "n_cases": len(cases),
        "n_eligible": len(eligible),
        "n_source": len(items),
        "n_documents": len(cases) * len(news_keys(task)),
        "strata": dict(strata_counts),
        "cases": cases,
    }
    (out_dir / "dataset.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    ood = {
        "version": "1.0",
        "purpose": "out-of-kb refusal",
        "note": f"留出集：{task} 的 {len(ood_items)} 条样本，对应新闻未写入 documents/，用于测拒答。",
        "seed": SEED,
        "n_cases": len(ood_items),
        "cases": [
            {
                "id": x["ID"],
                "event": (x.get("event") or "").strip(),
                "question": x["questions"].strip(),
                "answer": x["answers"].strip(),
                "document_in_pack": False,
            }
            for x in ood_items
        ],
    }
    (out_dir / "ood_questions.json").write_text(
        json.dumps(ood, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"task: {task}")
    print(f"eligible: {len(eligible)} / {len(items)} (filter={apply_filter})")
    print(f"in-kb: {len(cases)} strata={dict(strata_counts)}")
    print(f"ood: {len(ood_items)}")
    print(f"documents: {docs_dir} ({dataset['n_documents']} files)")

    return {
        "source_key": source_key,
        "n_source": len(items),
        "n_eligible": len(eligible),
        "n_cases": len(cases),
        "n_ood": len(ood_items),
        "n_documents": dataset["n_documents"],
        "strata": dict(strata_counts),
    }


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(out_root: Path, source: Path, subsets: dict, args) -> None:
    manifest_path = out_root / "manifest.json"
    existing: dict = {}
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    merged = dict(existing.get("subsets") or {})
    merged.update(subsets)

    manifest = {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(source),
        "source_sha256": file_sha256(source),
        "seed": SEED,
        "filter": {
            "enabled": not args.no_filter,
            "min_news_len": MIN_NEWS_LEN,
            "answer_support_required": not args.no_filter,
        },
        "requested": {"n_cases": args.n_cases, "ood_count": args.ood_count},
        "subsets": merged,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Kura-AI RAG eval packs")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="CRUD-RAG split_merged.json")
    parser.add_argument("--task", choices=("all", "1doc", "2doc", "3doc"), default="1doc")
    parser.add_argument(
        "--out-root",
        type=Path,
        default=ROOT,
        help="输出根目录；各子集写入 <out-root>/<task>/",
    )
    parser.add_argument(
        "--n-cases",
        type=int,
        default=DEFAULT_N_CASES,
        help="每个子集库内题数；0 表示取全部合格样本（扣除留出集）",
    )
    parser.add_argument("--ood-count", type=int, default=DEFAULT_OOD_COUNT, help="每子集留出集（OOD）题数")
    parser.add_argument("--no-filter", action="store_true", help="跳过合格性过滤（不校验新闻长度与答案可支撑）")
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        raise SystemExit(f"source not found: {source}")
    out_root = args.out_root.resolve()

    with source.open(encoding="utf-8") as f:
        raw = json.load(f)

    tasks = ["1doc", "2doc", "3doc"] if args.task == "all" else [args.task]
    subsets: dict = {}
    for task in tasks:
        subsets[task] = build_task(
            task,
            source,
            raw,
            out_root,
            n_cases=args.n_cases,
            ood_count=args.ood_count,
            apply_filter=not args.no_filter,
        )
        print()

    write_manifest(out_root, source, subsets, args)
    print(f"manifest: {out_root / 'manifest.json'}")


if __name__ == "__main__":
    main()