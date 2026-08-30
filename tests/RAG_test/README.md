# Kura-AI RAG 1doc 评测包

闭集知识库问答评测数据，从 [CRUD-RAG](https://github.com/IAAR-Shanghai/CRUD_RAG) 的 `questanswer_1doc` 抽取。本目录自包含，评测时不必再打开 CRUD-RAG 仓库。

当前规模：

- 入库文档 / 问答对：80
- 库外拒答题：10
- 合格池 / 源集：450 / 800
- 分层：`numeric` 40、`named` 25、`multi` 15
- 抽样种子：`42`

## 目录

```
tests/RAG_test/
  README.md
  dataset.json           # 80 条 in-kb 用例与元数据
  ood_questions.json     # 10 条库外拒答题（对应新闻未入库）
  documents/{id}.md      # 80 篇 Markdown，file_key = {id}.md
  _build.py              # 可复现构建脚本
```

## 字段

`dataset.json` 每条用例：

| 字段 | 含义 |
|---|---|
| `id` | CRUD-RAG 原始 ID |
| `event` | 事件摘要，也是文档标题 |
| `question` | 评测提问 |
| `answer` | 参考答案（事实对照用，不要拿来算 BLEU） |
| `document` | 相对路径，如 `documents/{id}.md` |
| `file_key` | 上传后应保持的文件名，选档/检索命中的 gold |
| `stratum` | `numeric` / `named` / `multi` |

`ood_questions.json` 每条有同样的 `question` / `answer`，但 `document_in_pack` 为 `false`：答案不在这 80 篇里。

文档正文是清洗过的 `news1`（压缩空白，不改写事实）。

## 推荐用法

单独建一个评测智能体，不要混进业务知识库。

1. 把 `documents/` 下 80 个 `.md` 全部上传到该智能体知识库。上传后文件名须仍是 `{id}.md`，以便和 `file_key` 对齐。当前上传接口若尚未支持 Markdown，等扩展完成后再灌库。
2. 关闭联网搜索，只开知识库检索。
3. 对 `dataset.json` 的每条 `question` 发一轮独立对话（不要带上一条的上下文）。
4. 记录：是否调用 `search_knowledge_base`、选档得到的 `file_key`、检索命中的文件名、是否拒答、最终回答。
5. 用 `ood_questions.json` 再问 10 题，期望如实说明「知识库中未找到相关资料」，而不是编造 `answer` 里的事实。

建议拆开看，不要合成一个字符串重叠分数：

- 选档是否包含该条 `file_key`
- 检索结果是否覆盖 gold 文档
- 回答是否覆盖 `answer` 中的关键事实（人工抽检或 LLM 判定）
- 库外题是否拒答

不要用 BLEU / ROUGE 对 Kura 带引用、口语化的回答打分，分数会虚低且改不了哪一层。

## 重建

需要 CRUD-RAG 的 `split_merged.json`：

```sh
python tests/RAG_test/_build.py
# 或
python tests/RAG_test/_build.py --source D:\LLMProjects\CRUD_RAG\data\crud_split\split_merged.json
```

改配额或种子后重跑会覆盖 `documents/`、`dataset.json`、`ood_questions.json`。
