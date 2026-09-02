"""从 CRUD-RAG 抽取 Kura-AI 闭集 RAG 评测包（1doc / 2doc / 3doc）。"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path

SEED = 42
N_CASES = 80
N_OOD = 10
MIN_NEWS_LEN = 200

STRATUM_QUOTA = {
    "multi": 15,
    "numeric": 40,
    "named": 25,
}

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


def is_eligible(item: dict, task: str) -> bool:
    question = (item.get("questions") or "").strip()
    answer = (item.get("answers") or "").strip()
    if not question or not answer or not (item.get("ID") or "").strip():
        return False
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


def stratified_sample(eligible: list[dict], rng: random.Random) -> list[dict]:
    by_stratum: dict[str, list[dict]] = defaultdict(list)
    for item in eligible:
        by_stratum[classify(item)].append(item)

    used: set[str] = set()
    selected: list[dict] = []
    for name, quota in STRATUM_QUOTA.items():
        selected.extend(take_stratum(by_stratum.get(name, []), quota, used, rng))

    if len(selected) < N_CASES:
        leftover = [x for x in eligible if x["ID"] not in used]
        rng.shuffle(leftover)
        need = N_CASES - len(selected)
        extra = leftover[:need]
        selected.extend(extra)
        used.update(x["ID"] for x in extra)

    rng.shuffle(selected)
    return selected[:N_CASES]


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


def build_task(task: str, source: Path, root: Path) -> None:
    source_key = TASK_SOURCE_KEY[task]
    with source.open(encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get(source_key) or []
    if not items:
        raise SystemExit(f"{source_key} is empty")

    eligible = [x for x in items if is_eligible(x, task)]
    if len(eligible) < N_CASES:
        raise SystemExit(f"{task}: eligible samples {len(eligible)} < {N_CASES}")

    rng = random.Random(SEED)
    selected = stratified_sample(eligible, rng)
    selected_ids = {x["ID"] for x in selected}

    unused = [x for x in eligible if x["ID"] not in selected_ids]
    rng.shuffle(unused)
    ood_items = unused[:N_OOD]
    if len(ood_items) < N_OOD:
        raise SystemExit(f"{task}: not enough unused samples for OOD: {len(ood_items)}")

    out_dir = root / task
    docs_dir = out_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for old in docs_dir.glob("*.md"):
        old.unlink()
    for item in selected:
        write_documents(docs_dir, item, task)

    cases = [to_case(x, task) for x in selected]
    strata_counts = defaultdict(int)
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
        "note": f"问题来自未入选的 {task} 样本，对应新闻未写入 documents/，用于测拒答。",
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
    print(f"source: {source}")
    print(f"eligible: {len(eligible)} / {len(items)}")
    print(f"in-kb: {len(cases)} strata={dict(strata_counts)}")
    print(f"ood: {len(ood_items)}")
    print(f"documents: {docs_dir} ({dataset['n_documents']} files)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Kura-AI RAG eval packs")
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="CRUD-RAG split_merged.json path",
    )
    parser.add_argument(
        "--task",
        choices=("1doc", "2doc", "3doc"),
        default="1doc",
        help="Which split to build (writes to tests/RAG_test/<task>/)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="RAG_test root directory",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        raise SystemExit(f"source not found: {source}")

    build_task(args.task, source, args.root.resolve())


if __name__ == "__main__":
    main()
