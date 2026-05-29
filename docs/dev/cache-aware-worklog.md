# Cache-Aware 超限路由 — 开发 Worklog

实时记录每一步：改了什么、为什么、怎么测、结果。每个提交一节。

- 分支：`sss-dev`
- 提交身份：`Saddss <2872669061@qq.com>`
- 计划：见 `.cursor/plans/`（cache-aware overload routing）

需求：
1. 排队阈值 `--cache-aware-tolerate-waiting-requests`
2. p50 TTFT 阈值
3. p99 TTFT 阈值
4. p50 端到端阈值
5. p99 端到端阈值
6. Prometheus 暴露（≥30s 窗口）：粘滞率、各阈值 fallback 率、总体 fallback 率
7. fallback 选择：候选满足全部阈值，按 `(queue 升序, p50_e2e 升序)` 确定性选择；全员超限退回原 engine
8. 近 0 性能损失：分位数 TTL 缓存、指标 O(1) 摊销更新

---

## C0 — 环境与合规准备

### 改了什么
- `git config --local`：`user.name=Saddss`、`user.email=2872669061@qq.com`。
- 新增 repo-local 规则 `.cursor/rules/no-cursor-in-commits.mdc`（禁止任何 Cursor 痕迹）。
- 新增本 worklog `docs/dev/cache-aware-worklog.md`。

### 怎么测
- `git config --local user.name/user.email` 回显校验。
- commit 后 `git log -1 --format='%an <%ae>%n%B' | grep -i cursor` 自检无输出。

### 结果
- 待提交后回填 commit hash 与自检结果。

---
