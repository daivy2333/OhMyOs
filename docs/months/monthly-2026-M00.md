# M00 - 训练营后启动实习中


> 关联周报：[`weekly-2026-W01`](../weeks/weekly-2026-W01.md)、[`weekly-2026-W02`](../weeks/weekly-2026-W02.md)、[`weekly-2026-W03`](../weeks/weekly-2026-W03.md)

## 当前工作

重心从异步串口本身，转到把 QEMU 路径中的假设逐步推到 Lichee RV Dock 上验证，并根据实际面临的各种问题对代码进行优化。

已经做了的：
1. Q15 增量重集成，稳定异步 UART 的核心改动。
2. 将原本单一的真板验证目标拆成 Q16~Q23，降低各路径之间的耦合。
3. 用 Lichee RV Dock 打通 D1 平台 smoke、kbench、userbench，完成 `/dev/console`、TTY、syscall、`tcdrain`、FIONBIO 的端到端验证。
这周下周将要做的：
1. 继续真板上的探索，对各种态的测试进行收尾
2. 确认测试的各种基准，保证qemu和真板可以横向对比，更新测试代码并测试

## 进展

| 阶段 | 时间 | 重点 | 结果 |
|------|------|------|------|
| Q15 收尾 | 2026-06-21 ~ 2026-06-27 | M0~M4 增量重集成、Manual QA、文档同步 | 性能回归风险下降，后续真板验证前置条件收敛（[`d5ef7d4`](https://github.com/daivy2333/StarryOS/commit/d5ef7d4)） |
| Q16~Q18 准备 | 2026-06-27 ~ 2026-06-28 | roadmap 拆分、平台参数解耦、early console 抽象 | QEMU UART facts 从驱动初始化路径中抽离（[`941ad05`](https://github.com/daivy2333/StarryOS/commit/941ad05)） |
| Q19 smoke | 2026-06-28 ~ 2026-06-29 | D1 axplat、boot image、early console、PLIC 前置处理 | 真板输出 `[starry-d1] smoke complete, halting.`（[`4a228be`](https://github.com/daivy2333/StarryOS/commit/4a228be)、[`afafb31`](https://github.com/daivy2333/StarryOS/commit/afafb31)） |
| Q19B userbench | 2026-06-29 起 | DW APB UART、axfs-ng patch、embedded benchmark loader | 真板跑通 userbench，并拿到 TX / tcdrain / FIONBIO 数据（[`c820567`](https://github.com/daivy2333/StarryOS/commit/c820567)、[`4ba2f75`](https://github.com/daivy2333/StarryOS/commit/4ba2f75)） |
| Q19C-M0 benchmark evidence cleanup | 2026-07-07 | benchmark.c 统一、64B 测量污染消除、`send_bytes` 16B FIFO burst、slow-pool + yield 重试 | D1 64B 11.13 KB/s（96.6% 线速），`slow_poll_exh=0`、`yield_exh=0`；P99 长尾根因未探明（[`7a13a46`](https://github.com/daivy2333/StarryOS/commit/7a13a46)、[`217fdd7`](https://github.com/daivy2333/StarryOS/commit/217fdd7)） |

## 产出

- 建立训练营后的学习记录站：`OhMyOs` 用 MkDocs Material + GitHub Pages 承载周报、月报和学习笔记。
- 完成 Q15 异步 UART 增量重集成：M0 witness layer（[`b1492a5`](https://github.com/daivy2333/StarryOS/commit/b1492a5)）、TX completion drain（[`4923cd2`](https://github.com/daivy2333/StarryOS/commit/4923cd2)）、TtyWrite short-write（[`529c414`](https://github.com/daivy2333/StarryOS/commit/529c414)）、IER single owner（[`4923cd2`](https://github.com/daivy2333/StarryOS/commit/4923cd2)）等改动的验证边界已明确。
- 完成 Q16~Q23 真板验证拆分，把"等硬件"改成可并行推进的前置探索、平台适配和 gate 验证（[`941ad05`](https://github.com/daivy2333/StarryOS/commit/941ad05)）。
- 在 Lichee RV Dock 上完成 D1 平台基础 bring-up（[`4a228be`](https://github.com/daivy2333/StarryOS/commit/4a228be)），解决 boot image、PTE 属性、IRQ stub、virtio/PCI 隔离等真板问题。
- 完成 Q19B embedded `benchmark.elf` userbench 路径（[`4ba2f75`](https://github.com/daivy2333/StarryOS/commit/4ba2f75)），验证异步 UART 与用户态 runtime 的关键链路。
- 完成 Q19C-M0 benchmark evidence cleanup：统一 QEMU/D1 benchmark manifest。消除 64B stdout backlog 测量污染；`send_bytes` 16B FIFO burst + OPOST/ONLCR short-write 修复；TX copier slow-pool + yield 重试（[`7a13a46`](https://github.com/daivy2333/StarryOS/commit/7a13a46)、[`217fdd7`](https://github.com/daivy2333/StarryOS/commit/217fdd7)）。

## 主要问题与修复

| 问题 | 表现 | 处理 |
|------|------|------|
| QEMU UART 常量散落 | 换板时只改 base 不够，D1 还需要 stride 4 + 32-bit MMIO | 引入 platform descriptor，驱动初始化只消费平台事实（[`941ad05`](https://github.com/daivy2333/StarryOS/commit/941ad05)） |
| D1 early DDR 属性缺失 | 真板启动出现 `Store/AMO access fault` | 为 C906 DDR 映射补 T-Head C9xx normal-memory 属性 bits（SH/B/C）（[`4a228be`](https://github.com/daivy2333/StarryOS/commit/4a228be)） |
| boot image 过大 | raw binary 超过 boot 分区 | 构建入口强制 `DWARF=n`（[`4a228be`](https://github.com/daivy2333/StarryOS/commit/4a228be)） |
| D1 无 virtio / PCI | smoke 路径触发 block / PCI 初始化崩溃 | 隔离 fs/net/display/axdriver/PCI/task-ext（[`e7d1933`](https://github.com/daivy2333/StarryOS/commit/e7d1933)） |
| userbench feature 继承错误 | 用户态路径模块被 kbench/smoke 排除规则误伤 | 拆分硬件能力 feature 与运行模式 feature（[`fcb008d`](https://github.com/daivy2333/StarryOS/commit/fcb008d)） |
| embedded ELF 带 relocation | loader 跳转到异常地址 | 使用 `-static -no-pie -fno-pie -s` 生成 ET_EXEC（[`4ba2f75`](https://github.com/daivy2333/StarryOS/commit/4ba2f75)） |
| THRE 边沿丢失 | 真板 `tcdrain` 不返回 | 启用 THRE 时基于 LSR 软件补 wake（[`b1d15e3`](https://github.com/daivy2333/StarryOS/commit/b1d15e3)） |
| drain 覆盖不完整 | TX staged / TEMT 变化未唤醒等待者 | `flush()` 注册 `DRAIN_WAKER`，TX copier 在最后阶段主动 wake（[`b1d15e3`](https://github.com/daivy2333/StarryOS/commit/b1d15e3)） |
| 64B stdout backlog 测量污染 | D1 64B `write+tcdrain` 约 1 KB/s（8.8% 线速） | 每节开始加 `fflush(stdout); tcdrain(STDOUT_FILENO)` 并打印 pre-drain。隔离后 D1 64B 达 11.13 KB/s（96.6% 线速）（[`7a13a46`](https://github.com/daivy2333/StarryOS/commit/7a13a46)） |
| `send_bytes` 单字节发送 | `hw_send_max_chunk` 限为 1，1024B S11 正确发送失败 | 启用 THRE 后一次填最多 16B FIFO；`TX_FIFO_SIZE=16`（[`7a13a46`](https://github.com/daivy2333/StarryOS/commit/7a13a46)） |
| OPOST/ONLCR short-write 计数错误 | S11 1024B `short_writes` 高数字 | `Tty::write` 用 `complete_at_prefix` 数组精确定位 short-write 边界（[`7a13a46`](https://github.com/daivy2333/StarryOS/commit/7a13a46)） |
| TX copier budget exhausted 卡死 | fast retry 32 次失败后无 fallback，benchmark 启动卡住 | 引入 bounded slow-pool（`TX_SLOW_POLL_LIMIT=4096` × `TX_SLOW_POLL_SPINS=256`），失败后再 yield 重试（`TX_YIELD_RETRIES=4`）（[`217fdd7`](https://github.com/daivy2333/StarryOS/commit/217fdd7)） |

## 实习初步计划

训练营结束，进入实习阶段。初步计划分三阶段：

**第一阶段**：在 Lichee RV Dock（D1）上将异步串口驱动完整跑通，给出性能测试数据。

**第二阶段**：移植到 K3 开发板，在 StarryOS 上使串口工作，支持多核。荔枝派的经验加上前人的探索资料应能加速推进；多核环境会暴露新问题。该阶段完成时，对异步串口开发做总结。后续整理一份荔枝派开发资料。

**第三阶段**：参与 K3 上网络驱动相关工作。第二阶段完成后，应能更好地理解该工作该怎么进行，当前也不敢说大话。

**再后面**：就是我其实是有继续实习下去的想法的，不过训练营支不支持，几个个月后我还有没有这个想法，我能力够不够，各种问题只能由时间来解答了。

## 参考

- [`weekly-2026-W01`](../weeks/weekly-2026-W01.md)：Q15 收尾 + Q6/Q16/Q17 准备
- [`weekly-2026-W02`](../weeks/weekly-2026-W02.md)：Lichee RV Dock 真板 smoke 完成
- [`weekly-2026-W03`](../weeks/weekly-2026-W03.md)：Q19B userbench 完成 + Q19C-M0 benchmark evidence cleanup
