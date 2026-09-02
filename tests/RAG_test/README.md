# Kura-AI RAG 评测包

闭集知识库问答数据，从 [CRUD-RAG](https://github.com/IAAR-Shanghai/CRUD_RAG) 抽取。本目录自包含，评测时不必再打开 CRUD-RAG 仓库。

三套任务各自独立，**必须分智能体灌库**，不要混传，也不要混进业务知识库。

| 子集 | 问答 | 入库文档 | 库外拒答题 | 合格池 / 源集 |
|---|---|---|---|---|
| [1doc/](1doc/) | 80 | 80（一篇一问） | 10 | 450 / 800 |
| [2doc/](2doc/) | 80 | 160（两篇一问） | 10 | 617 / 797 |
| [3doc/](3doc/) | 80 | 240（三篇一问） | 10 | 663 / 797 |

分层均为 `numeric` 40、`named` 25、`multi` 15；抽样种子 `42`。1doc 为既有抽样子集（搬家未重抽）。2doc / 3doc 正文与 1doc **不是同一批新闻**，即使 `id` 相同也不能复用文件。

## 目录

```
tests/RAG_test/
  README.md
  _build.py
  1doc/  dataset.json  ood_questions.json  documents/{id}.md
  2doc/  dataset.json  ood_questions.json  documents/{id}_news1.md  {id}_news2.md
  3doc/  dataset.json  ood_questions.json  documents/{id}_news1.md  {id}_news2.md  {id}_news3.md
```

## 字段

1doc 每条：`id`、`event`、`question`、`answer`、`document`、`file_key`（字符串）、`stratum`。

2doc / 3doc 每条：

| 字段 | 含义 |
|---|---|
| `id` | CRUD-RAG 原始 ID |
| `event` | 事件摘要 |
| `question` | 评测提问（多源综合） |
| `answer` | 参考答案（事实对照，不要算 BLEU） |
| `documents` | 相对路径列表 |
| `file_keys` | 上传后应保持的文件名列表，选档/检索 gold |
| `stratum` | `numeric` / `named` / `multi` |

`ood_questions.json` 问题真实，但 `document_in_pack` 为 `false`：对应新闻未写入该子集的 `documents/`。

文档标题：1doc 为 `# {event}`；2doc/3doc 为 `# {event}（news1）` 等。正文是清洗过的 `newsN`（压缩空白，不改写事实）。

## 推荐用法

1. 每个子集单独建评测智能体。把该子集 `documents/` 下全部 `.md` 上传，**文件名保持与 `file_keys` 一致**。
2. 关闭联网搜索，只开知识库检索。
3. 对 `dataset.json` 每条 `question` 发一轮独立对话（不要带上一条上下文）。
4. 记录：是否调用 `search_knowledge_base`、选档得到的 file_key、检索命中文件名、是否拒答、最终回答。
5. 再用该子集的 `ood_questions.json` 测拒答，期望说明「知识库中未找到相关资料」。

建议拆开看：

- 1doc：选档/检索是否命中该条 `file_key`
- 2doc / 3doc：检索结果是否覆盖该条 **全部** `file_keys`，回答是否覆盖多源事实
- 库外题是否拒答

不要用 BLEU / ROUGE 打分。

### 选档条数上限

Kura 选档默认最多列出约 100 个 `file_key`（`KB_PRESELECT_MAX_DOC_LINES`）。1doc 80 篇一般够列全；2doc 约 160、3doc 约 240 会超出。测 2doc / 3doc 时请关闭选档（`KB_DOCUMENT_PRESELECT_ENABLED=false`），或接受「模型未圈定则全库检索」的回退。

## 重建

```sh
python tests/RAG_test/_build.py --task 1doc
python tests/RAG_test/_build.py --task 2doc
python tests/RAG_test/_build.py --task 3doc
```

会覆盖对应子目录的 `documents/`、`dataset.json`、`ood_questions.json`。1doc 已测过，不要随意重跑，除非有意换样本。
