# COLLAB_LOG — 协同改动日志（ZCode × workbuddy）

> 本仓库由 ZCode 与 workbuddy 两个 AI 会话协同改动。规则（两边都要遵守）：
> 1. **开工先读本文件**：看对方最近的改动与待办，避免撞车、重复劳动或裹入对方在途文件；
> 2. **改完必追加**：每次改动完成（或告一段落）在文件**末尾**追加一条；
> 3. **只追加，不改不删**别人写的条目；有异议就在自己条目里写「回应」；
> 4. 提交一律显式 `git add <路径>`（HANDOFF.md 既有约定）；运行期脏文件（如 `backend/spot_media.json`）不裹入。
>
> 条目格式：`## YYYY-MM-DD HH:mm | 谁 | 主题`，正文写：改了什么 / 涉及文件 / 门禁状态 / 风险与待办。

---

## 2026-10-02 01:25 | ZCode | R3 检索评测脚手架入库（f435759）

- **改了什么**：RAG 模块 R3 检索评测——golden 30 条（`backend/rag_golden.json`）、评测脚本 `backend/eval_rag.py`（BM25 基线臂 + 向量余弦臂，语料向量磁盘缓存 `data/rag_embed_cache.json`）、指标纯函数单测（`backend/tests/test_rag_eval.py`，4 条）、结果 `backend/eval_rag.json`。**BM25 基线：hit@1 66.7% / hit@5 70.0% / MRR 0.689**。
- **顺手修**：eval_rag.py 过 mypy（TYPE_CHECKING 引 OpenAI、补 logger 与降级注释）；`tools/check_backend_health.py` 豁免清单加 `_llm_fast`（只构造客户端，重试在 `_embed_cached` 自带 3 次退避）。
- **门禁**：pytest 424 绿 ｜ ruff 绿 ｜ mypy 0 错 ｜ 健康 0 错 0 警 ｜ 前端静态 + 对比度绿 ｜ 双冒烟 22/22 + 48/48 绿。
- **另修**：`HANDOFF.md` Node 路径过期（22.22.2-3 → **22.22.2-5**，且实测不在 PATH 须全路径）。
- **风险与待办**：① **向量臂被智谱 embedding 429（余额不足）阻塞**，`eval_rag.json` 的 vector 字段是 error 记录、不是最终结果，出路待用户拍板（充值 / 换通道 / 本地替代）；② R3 对照结论将补进 `ROADMAP.md` 与 `docs/interview-defense.md`（下一条目）；③ 后端服务未动（`rag.py` 无改动），无需重启。

## 2026-10-02 02:10 | ZCode | R3 完结：向量臂落地 + 三臂对照定论（8ce2f83）

- **改了什么**：向量臂打通——embedding 通道可配置（`EMBED_API_KEY/EMBED_BASE_URL/EMBED_MODEL` 优先、回退 `LLM_FAST_*`；本机走硅基流动 `BAAI/bge-m3` 免费档，Key 在 `backend/.env` 的 `EMBED_*`，gitignore 内不提交）；向量缓存按模型隔离（换模型自动重建，`data/rag_embed_cache.json`）；补第三臂 `eval_rerank`（BM25 top-20 → 向量重排，评审立项口径）。
- **对照结论（golden 30 条，`backend/eval_rag.json`）**：BM25 hit@1 66.7% / hit@5 70.0% / MRR 0.689 ｜ BM25 top-20→向量重排 60.0 / 70.0 / 0.647 ｜ 纯向量全库 63.3 / 76.7 / 0.704。**向量重排全指标劣化 → 不采纳，维持 BM25**。互补性在案：BM25 有 8 条 top-10 全盲区、向量全找回（含 1 条 hit@1）但 4 条深排 39~118 名；重测条件 = 语料 desc 增强。
- **文档**：`ROADMAP.md`（立项块标注结论 + 更新记录补 10-01~02 条目）、`docs/interview-defense.md`（补「为什么不上向量」标准答法）。
- **门禁**：pytest 424 绿 ｜ ruff 绿 ｜ mypy 0 错 ｜ 健康 0 错 0 警（全部本批复跑）。
- **风险与待办**：① 语料 desc 空洞是检索指标天花板的结构性原因，语料增强优先级高于向量方案；② RRF 融合未测（列为后续选项，不主动做）；③ 提交均未 push（等用户授权）；④ `backend/spot_media.json` 仍是运行期脏文件，未裹入任何提交。
