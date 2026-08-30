"""从 CRUD-RAG questanswer_1doc 抽取 Kura-AI 闭集 RAG 评测包。"""

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

DEFAULT_SOURCE = Path(r"D:\LLMProjects\CRUD_RAG\data\crud_split\split_merged.json")

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


def answer_supported(answer: str, news1: str) -> bool:
    parts = [p.strip() for p in _SPLIT_ANSWER_RE.split(answer) if len(p.strip()) >= 2]
    parts = [p for p in parts if p not in _SKIP_ANSWER_PARTS]
    if not parts:
        return any(token in news1 for token in re.findall(r"[\u4e00-\u9fff]{2,}", answer)[:8])
    return any(p in news1 for p in parts)


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


def is_eligible(item: dict) -> bool:
    news = clean_news(item.get("news1") or "")
    question = (item.get("questions") or "").strip()
    answer = (item.get("answers") or "").strip()
    if len(news) < MIN_NEWS_LEN or not question or not answer:
        return False
    if not (item.get("ID") or "").strip():
        return False
    return answer_supported(answer, news)


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


def to_case(item: dict, *, include_stratum: bool = True) -> dict:
    file_key = f"{item['ID']}.md"
    case = {
        "id": item["ID"],
        "event": (item.get("event") or "").strip(),
        "question": item["questions"].strip(),
        "answer": item["answers"].strip(),
        "document": f"documents/{file_key}",
        "file_key": file_key,
    }
    if include_stratum:
        case["stratum"] = classify(item)
    return case


def write_document(out_dir: Path, item: dict) -> None:
    event = (item.get("event") or "").strip() or item["ID"]
    body = clean_news(item["news1"])
    path = out_dir / f"{item['ID']}.md"
    path.write_text(f"# {event}\n\n{body}\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Kura-AI RAG 1doc eval pack")
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="CRUD-RAG split_merged.json path",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Output directory (tests/RAG_test)",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        raise SystemExit(f"source not found: {source}")

    with source.open(encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("questanswer_1doc") or []
    if not items:
        raise SystemExit("questanswer_1doc is empty")

    eligible = [x for x in items if is_eligible(x)]
    if len(eligible) < N_CASES:
        raise SystemExit(f"eligible samples {len(eligible)} < {N_CASES}")

    rng = random.Random(SEED)
    selected = stratified_sample(eligible, rng)
    selected_ids = {x["ID"] for x in selected}

    unused = [x for x in eligible if x["ID"] not in selected_ids]
    rng.shuffle(unused)
    ood_items = unused[:N_OOD]
    if len(ood_items) < N_OOD:
        raise SystemExit(f"not enough unused samples for OOD: {len(ood_items)}")

    out_dir = args.out_dir.resolve()
    docs_dir = out_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for old in docs_dir.glob("*.md"):
        old.unlink()
    for item in selected:
        write_document(docs_dir, item)

    cases = [to_case(x) for x in selected]
    strata_counts = defaultdict(int)
    for c in cases:
        strata_counts[c["stratum"]] += 1

    dataset = {
        "version": "1.0",
        "source": "CRUD-RAG questanswer_1doc",
        "source_file": str(source),
        "task": "1doc",
        "seed": SEED,
        "n_cases": len(cases),
        "n_eligible": len(eligible),
        "n_source": len(items),
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
        "note": "问题来自未入选的 1doc 样本，对应新闻未写入 documents/，用于测拒答。",
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

    print(f"source: {source}")
    print(f"eligible: {len(eligible)} / {len(items)}")
    print(f"in-kb: {len(cases)} strata={dict(strata_counts)}")
    print(f"ood: {len(ood_items)}")
    print(f"documents: {docs_dir}")


if __name__ == "__main__":
    main()
