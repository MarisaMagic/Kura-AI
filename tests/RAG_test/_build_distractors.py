"""为 RAG 评测包生成「同话题近邻」干扰文档（hard 版）。

为什么需要专门的选取逻辑：
- CRUD-RAG 的 80000_docs 语料与 questanswer 的 gold 新闻**同源**。实测 3065 条 gold 中有
  1907 条的完整版就在该语料里（约 6770 行）。若直接把这些行当干扰文档入库，等于把答案
  以另一个文件名放进知识库，实验结论无法解释——因此必须做泄漏剔除。
- 随机抽的无关新闻几乎不会挤进 top-k，加了等于没加。这里按「与 gold 共享的稀有特征」
  打分，选取话题相近的新闻，才能真正制造排序竞争。
- 需控制主题广度：1000 篇若全来自同一热点事件簇，干扰高度同质，同样没有意义。故按
  首选特征分簇并限制每簇配额。

用法（Kura-AI 仓库根目录）：
    python tests/RAG_test/_build_distractors.py \
        --full-root D:\\LLMProjects\\Kura-RAG-eval-full \
        --out-root  D:\\LLMProjects\\Kura-RAG-eval-hard \
        --per-task 1000
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CORPUS = Path(r"D:\LLMProjects\CRUD_RAG\data\80000_docs")
DEFAULT_SPLIT = Path(r"D:\LLMProjects\CRUD_RAG\data\crud_split\split_merged.json")
DEFAULT_FULL_ROOT = Path(r"D:\LLMProjects\Kura-RAG-eval-hard").parent / "Kura-RAG-eval-full"

TASKS = ("1doc", "2doc", "3doc")

# 泄漏判定：候选行含 gold 的归一化前缀（越长越严格）
LEAK_PREFIX_CHARS = 60
# 候选行过短则无法有效分块，不选
MIN_DISTRACTOR_CHARS = 150

# 实体样特征：机构 / 地名 / 书名号 / 引号内专名 / 数字串
_ENTITY_PATTERNS = (
    re.compile(r"《[^》]{2,30}》"),
    re.compile(r"[\u4e00-\u9fff]{2,10}(?:部|委|局|署|厅|院|会|社|集团|公司|银行|大学|学院|中心|协会|委员会|管理局|交易所|研究院|事务所)"),
    re.compile(r"[\u4e00-\u9fff]{2,8}(?:省|市|县|区|州|镇|乡)"),
    re.compile(r"[\u4e00-\u9fff]{2,6}(?:号|条例|规定|办法|通知|意见|方案|报告|规划|法)"),
    re.compile(r"\d{2,}[\u4e00-\u9fff%万千亿美港台元]{0,6}"),
)
_WS_RE = re.compile(r"\s+")

# 中文 bigram：无需分词即可刻画话题，与实体特征互补
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
# 这些 bigram 在任何新闻里都高频，无区分度
_STOP_BIGRAMS = frozenset(
    [
        "记者",
        "报道",
        "日电",
        "通讯",
        "本报",
        "表示",
        "指出",
        "认为",
        "进行",
        "相关",
        "工作",
        "方面",
        "问题",
        "情况",
        "记者",
        "有关",
        "负责",
        "目前",
        "已经",
        "同时",
        "以及",
        "通过",
        "因为",
        "所以",
        "我们",
        "他们",
        "这个",
        "一个",
        "中国",
        "国家",
        "政府",
        "部门",
        "单位",
        "企业",
        "活动",
        "服务",
        "建设",
        "发展",
    ]
)


def normalize(text: str) -> str:
    return _WS_RE.sub("", text or "")


def load_corpus(shard_dir: Path) -> list[str]:
    """读取全部分片，按行拆成单篇新闻并去重（保留首个出现顺序）。"""
    seen: set[str] = set()
    lines: list[str] = []
    for p in sorted(shard_dir.glob("*")):
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = raw.strip()
            if not s or len(s) < MIN_DISTRACTOR_CHARS:
                continue
            key = normalize(s)
            if key in seen:
                continue
            seen.add(key)
            lines.append(s)
    return lines


def load_gold_texts(split_path: Path) -> list[str]:
    """CRUD-RAG 全部 questanswer 的 news 文本，作为泄漏检测基准。"""
    data = json.loads(split_path.read_text(encoding="utf-8"))
    out: list[str] = []
    for key in ("questanswer_1doc", "questanswer_2docs", "questanswer_3docs"):
        for item in data.get(key) or []:
            for nk in ("news1", "news2", "news3"):
                t = item.get(nk)
                if t:
                    out.append(normalize(t))
    return out


def leaked_indices(corpus: list[str], gold_texts: list[str]) -> set[int]:
    """
    找出「内容与某条 gold 高度重合」的候选行下标。
    判定：候选行归一化文本包含 gold 的归一化前 LEAK_PREFIX_CHARS 字符。
    用全局拼接 + bisect 定位，避免 O(n×m) 逐行比对。
    """
    norm_lines = [normalize(l) for l in corpus]
    offsets: list[int] = []
    pos = 0
    for l in norm_lines:
        offsets.append(pos)
        pos += len(l) + 1  # 与 joined 的 \n 分隔一致
    joined = "\n".join(norm_lines)

    leaked: set[int] = set()
    for g in gold_texts:
        if len(g) < LEAK_PREFIX_CHARS:
            continue
        prefix = g[:LEAK_PREFIX_CHARS]
        start = 0
        while True:
            i = joined.find(prefix, start)
            if i < 0:
                break
            row = bisect.bisect_right(offsets, i) - 1
            if 0 <= row < len(corpus):
                leaked.add(row)
            start = i + 1
    return leaked


def bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for seg in _CJK_RE.findall(text):
        for i in range(len(seg) - 1):
            bg = seg[i : i + 2]
            if bg not in _STOP_BIGRAMS:
                out.add(bg)
    return out


def entity_features(text: str) -> set[str]:
    out: set[str] = set()
    for pat in _ENTITY_PATTERNS:
        for m in pat.findall(text):
            out.add(m)
    return out


def build_feature_index(corpus: list[str]) -> tuple[list[set[str]], dict[str, int]]:
    """返回 (每篇候选的特征集合, 特征文档频率)。"""
    feats: list[set[str]] = []
    df: dict[str, int] = defaultdict(int)
    for line in corpus:
        f = entity_features(line) | bigrams(line)
        feats.append(f)
        for x in f:
            df[x] += 1
    return feats, dict(df)


def idf_weights(df: dict[str, int], total: int) -> dict[str, float]:
    """稀有特征权重更高；过高频（无区分度）与过低频（多为噪声）特征降权。"""
    w: dict[str, float] = {}
    for k, v in df.items():
        if v < 3:
            w[k] = 0.0
        else:
            w[k] = math.log(1.0 + total / v)
    return w


def select_distractors(
    corpus: list[str],
    feats: list[set[str]],
    weights: dict[str, float],
    gold_feature_set: set[str],
    banned: set[int],
    n: int,
    per_cluster_cap: int,
) -> list[int]:
    """
    按与 gold 的加权特征重叠打分，选取 n 篇；用「首选特征」分簇限制单簇配额，保证主题广度。
    """
    scored: list[tuple[float, int, str]] = []
    for i, f in enumerate(feats):
        if i in banned:
            continue
        overlap = f & gold_feature_set
        if not overlap:
            continue
        score = 0.0
        best_w = 0.0
        best_f = ""
        for x in overlap:
            w = weights.get(x, 0.0)
            if w <= 0.0:
                continue
            score += w
            if w > best_w:
                best_w = w
                best_f = x
        if score <= 0.0:
            continue
        scored.append((score, i, best_f))

    scored.sort(key=lambda t: t[0], reverse=True)

    picked: list[int] = []
    cluster_count: dict[str, int] = defaultdict(int)
    for score, idx, cluster in scored:
        if cluster_count[cluster] >= per_cluster_cap:
            continue
        picked.append(idx)
        cluster_count[cluster] += 1
        if len(picked) >= n:
            break

    # 簇配额可能不足以凑满 n：放宽配额按分数补齐
    if len(picked) < n:
        chosen = set(picked)
        for score, idx, _cluster in scored:
            if idx in chosen:
                continue
            picked.append(idx)
            chosen.add(idx)
            if len(picked) >= n:
                break

    return picked


def doc_body(gold_md: str) -> str:
    """从 gold 的 md 里取出正文（去掉首行标题）。"""
    lines = gold_md.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def sha256_dir_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()


def build_task(
    task: str,
    full_root: Path,
    out_root: Path,
    corpus: list[str],
    feats: list[set[str]],
    weights: dict[str, float],
    banned: set[int],
    per_task: int,
    per_cluster_cap: int,
) -> dict:
    src_dir = full_root / task / "documents"
    if not src_dir.is_dir():
        raise SystemExit(f"缺少 {src_dir}")

    out_dir = out_root / task
    docs_dir = out_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for old in docs_dir.glob("*.md"):
        old.unlink()

    # 1) 原样复制 gold 文档（题集与文件名一字不改）
    gold_files = sorted(src_dir.glob("*.md"))
    for p in gold_files:
        (docs_dir / p.name).write_bytes(p.read_bytes())

    # 2) 该子集 gold 的特征集合
    gold_feats: set[str] = set()
    for p in gold_files:
        body = doc_body(p.read_text(encoding="utf-8"))
        gold_feats |= entity_features(body) | bigrams(body)

    # 3) 选取干扰并写入
    picked = select_distractors(
        corpus, feats, weights, gold_feats, banned, per_task, per_cluster_cap
    )
    for n, idx in enumerate(picked, 1):
        text = corpus[idx]
        title = text[:40].replace("\n", " ")
        (docs_dir / f"dist_{n:05d}.md").write_text(
            f"# {title}\n\n{text}\n", encoding="utf-8"
        )

    # 4) 题集直接复用 full 包，保证与纯净版逐字节一致
    for name in ("dataset.json", "ood_questions.json"):
        src = full_root / task / name
        if src.is_file():
            (out_dir / name).write_bytes(src.read_bytes())

    print(
        f"{task}: gold={len(gold_files)} distractors={len(picked)} "
        f"docs_total={len(gold_files) + len(picked)}",
        flush=True,
    )
    return {
        "n_gold_docs": len(gold_files),
        "n_distractors": len(picked),
        "n_documents": len(gold_files) + len(picked),
        "gold_feature_count": len(gold_feats),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成同话题近邻干扰评测包")
    parser.add_argument("--full-root", type=Path, default=DEFAULT_FULL_ROOT)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--split", type=Path, default=DEFAULT_SPLIT)
    parser.add_argument("--per-task", type=int, default=1000, help="每个子集干扰文档数")
    parser.add_argument(
        "--per-cluster-cap",
        type=int,
        default=25,
        help="单个主题簇最多入选篇数（保证主题广度）",
    )
    parser.add_argument("--tasks", default=",".join(TASKS))
    args = parser.parse_args()

    full_root = args.full_root.resolve()
    out_root = args.out_root.resolve()
    corpus_dir = args.corpus.resolve()
    if not full_root.is_dir():
        raise SystemExit(f"全量包不存在: {full_root}")
    if not corpus_dir.is_dir():
        raise SystemExit(f"干扰语料不存在: {corpus_dir}")

    print(f"[1/4] 载入干扰语料 {corpus_dir}")
    corpus = load_corpus(corpus_dir)
    print(f"      候选 {len(corpus)} 篇（已去重、已过滤短行）", flush=True)

    print("[2/4] 泄漏检测：剔除包含 gold 内容的候选行")
    gold_texts = load_gold_texts(args.split.resolve())
    banned = leaked_indices(corpus, gold_texts)
    print(f"      gold 文本 {len(gold_texts)} 条；泄漏行 {len(banned)} 篇已剔除", flush=True)

    print("[3/4] 构建特征索引与 IDF 权重")
    feats, df = build_feature_index(corpus)
    weights = idf_weights(df, len(corpus))
    print(f"      特征 {len(df)} 个", flush=True)

    print("[4/4] 逐子集选取干扰并写出")
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    subsets: dict = {}
    for task in tasks:
        subsets[task] = build_task(
            task,
            full_root,
            out_root,
            corpus,
            feats,
            weights,
            banned,
            args.per_task,
            args.per_cluster_cap,
        )

    manifest_path = out_root / "manifest.json"
    manifest = {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "topical-nearest",
        "purpose": "在同话题近邻干扰下评测检索抗噪能力",
        "base_pack": str(full_root),
        "corpus_dir": str(corpus_dir),
        "corpus_shards": len(list(corpus_dir.glob("*"))),
        "corpus_lines_used": len(corpus),
        "leak_check": {
            "gold_texts": len(gold_texts),
            "leak_prefix_chars": LEAK_PREFIX_CHARS,
            "leaked_lines_excluded": len(banned),
        },
        "params": {
            "per_task": args.per_task,
            "per_cluster_cap": args.per_cluster_cap,
        },
        "subsets": subsets,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"manifest: {manifest_path}")


if __name__ == "__main__":
    main()