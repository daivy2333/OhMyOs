# W01 - Q15 收尾 + Q6/Q16/Q17 准备

**周期**：2026-06-21 ~ 2026-06-27

## Q15 增量重集成（M0~M4）

| 日期 | commit | 内容 |
|------|--------|------|
| 6/21 | [`05291f2`](https://github.com/daivy2333/StarryOS/commit/05291f2) | rollback 到 pre-M4 baseline |
| 6/23 | [`b1492a5`](https://github.com/daivy2333/StarryOS/commit/b1492a5) | M0 witness layer + FIFO boundary matrix benchmark |
| 6/23 | [`4923cd2`](https://github.com/daivy2333/StarryOS/commit/4923cd2) | M2 TX completion drain + M4 IER single owner 接入 StarryOS kernel |
| 6/23 | [`529c414`](https://github.com/daivy2333/StarryOS/commit/529c414) | M3 TtyWrite short-write contract + analysis 归档清理 |
| 6/24 | [`d5ef7d4`](https://github.com/daivy2333/StarryOS/commit/d5ef7d4) | M0~M4 标记完成 |

M0~M4 走增量路径，性能回归风险下降。

## Q15 文档同步与清理

- [`bb011e3`](https://github.com/daivy2333/StarryOS/commit/bb011e3)：Q15 Manual QA 状态同步到 SNAPSHOT / tasks / architecture / learned / optimization
- [`bc544c7`](https://github.com/daivy2333/StarryOS/commit/bc544c7)：补 Q13+ evolution report
- [`333e667`](https://github.com/daivy2333/StarryOS/commit/333e667)：3 份 report (architecture / benchmark / performance) 更新 Q15 状态
- [`36cacd6`](https://github.com/daivy2333/StarryOS/commit/36cacd6)：bitflags 2.11.0 → 2.13.0 (lockfile)
- [`59ceb8d`](https://github.com/daivy2333/StarryOS/commit/59ceb8d)：archivist 删除 learned.md 2 条 STALE 条目
- [`3ed54c3`](https://github.com/daivy2333/StarryOS/commit/3ed54c3)：OpenSpec 规范补 MUST/SHALL 关键字 + Scenario + 2-trait note

## Q6 真板验证前置探索

[`f17a8d5`](https://github.com/daivy2333/StarryOS/commit/f17a8d5)：调研 arceos 借鉴点（裸机/异构）+ 内存序验证方法 + 修订 ADR。产出 439 行 analysis + 5 份文档更新。

## Q15 后规划

[`1d6f7d2`](https://github.com/daivy2333/StarryOS/commit/1d6f7d2)：优化 milestone 重新规划。产出 `optimization-milestone-replan.md`（206 行）。

## Q16 / Q17 文档收敛

- [`e2f060e`](https://github.com/daivy2333/StarryOS/commit/e2f060e) (6/27)：Q16 roadmap — SNAPSHOT / tasks / 4 份 spec 同步
- [`e9af446`](https://github.com/daivy2333/StarryOS/commit/e9af446) (6/27)：Q17 SMP 内存序修复 — 103 行 analysis + 4 份 spec 更新
- [`58f718d`](https://github.com/daivy2333/StarryOS/commit/58f718d) (6/27)：Q17 change proposal 准备（design / proposal / spec / tasks）

## 关键事件

**性能回归触发重集成路径选择**

post-M4 出现性能回归。M0~M4 走增量路径替代一次性合入。M0 先建 witness layer 做 benchmark 证据链，后续 milestone 各自带 benchmark 验证。

**Q6 真板验证从「等硬件」转向「前置探索」**

[`f17a8d5`](https://github.com/daivy2333/StarryOS/commit/f17a8d5) 启动 Q6 前置探索：调研 arceos 借鉴点 + 内存序验证方法 + 修订 ADR。

## 后续

- Q17 SMP 内存序修复：change proposal 已就位，待启动 implementation
- Q6 真板验证：等硬件到位
- Q15 后优化：按 `optimization-milestone-replan.md` 推进

## 其他的

这个OhMyOs仓库，就用来记录学习操作系统的进度和挂在线文档的仓库吧。
下载了明扬的仓库，做了一些简单分析在这个地方[`f17a8d5`](https://github.com/daivy2333/StarryOS/commit/f17a8d5)。
继续学习rust的特性和操作系统的知识，加深对我写的串口的理解，以及实际代码实现的理解。
