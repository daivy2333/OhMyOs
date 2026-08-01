# 学习周记

按周/月记录学习进度、产出与思考。

- 训练营期间的学习记录请见 [2026sOsReport](https://github.com/daivy2333/2026sOsReport)
- 本仓库从训练营结束后开始持续更新，用于沉淀学习轨迹

## 周报索引

<!-- WEEKLY_INDEX_START -->
| 编号 | 主题 |
| --- | --- |
| [W06](weeks/weekly-2026-W06) | W06 - 网卡主线：同步基线 + 轮询基线 + IRQ 诊断启动 |
| [W05](weeks/weekly-2026-W05) | W05 - Console polling 基线 + CPU 效率对比 |
| [W04](weeks/weekly-2026-W04) | W04 - 异步串口契约收敛 + benchmark 补测收尾 |
| [W03](weeks/weekly-2026-W03) | W03 - Q19B userbench → Q19C 全链路收尾 |
| [W02](weeks/weekly-2026-W02) | W02 - Lichee RV Dock 真板 smoke 完成（Q16-Q19） |
| [W01](weeks/weekly-2026-W01) | W01 - Q15 收尾 + Q6/Q16/Q17 准备 |
| [W00](weeks/weekly-2026-W00) | W00 - 训练营后第一周 |
<!-- WEEKLY_INDEX_END -->

## 月报索引

<!-- MONTHLY_INDEX_START -->
| 编号 | 主题 |
| --- | --- |
| [M00](months/monthly-2026-M00) | M00 - 训练营后启动实习中 |
<!-- MONTHLY_INDEX_END -->

## 学习笔记

<!-- NOTES_INDEX_START -->
| 标题 | 标签 |
| --- | --- |
| [Async UART 与 polling Console 性能对比](notes/async-vs-console-performance) | uart, async, console, polling, performance, benchmark, qemu, d1, latency, throughput |
| [Async/Await 异步运行时](notes/async-await-runtime) | rust, async, future, waker, executor, state-machine |
| [DMA：绕过 CPU 的数据搬运](notes/dma-intro) | dma, hardware, uart, nic, cache, pio, descriptor, starryos |
| [ISR 极简 4 步流程](notes/isr-minimal-4-step) | rust, isr, riscv, interrupt, uart, dependency-injection |
| [ISR 触发后禁用中断](notes/isr-disable-level-trigger) | rust, os, riscv, interrupt, uart |
| [Lichee Q19B kbench/userbench 问题与解决](notes/lichee-q19b-benchmark-problems-solutions) |  |
| [Lichee RV Dock Smoke 前问题清单](notes/lichee-smoke-problems) |  |
| [Lichee RV Dock Smoke 解决路径](notes/lichee-smoke-solutions) |  |
| [Lichee RV Dock 适配前期准备](notes/lichee-adaptation-prework) |  |
| [MMIO：用 load/store 指令操作硬件](notes/mmio-intro) | mmio, hardware, volatile, uart, memory, pma |
| [OS 抽象具体实现](notes/async-os-abstraction) | rust, os, abstraction, trait, async |
| [Rust 异步驱动语言特性](notes/rust-async-driver-basics) | rust, async, generic, unsafe, smart-pointer, send, sync, deref |
| [TTY 阻塞读与 ProcessMode 桥接](notes/async-tty-processmode) | rust, tty, ldisc, async, waker |
| [VFS 接口 + flush 实现路径](notes/async-vfs-flush-path) | rust, vfs, tty, flush, syscall |
| [VirtIO 网卡队列机制入门](notes/virtio-net-queue-intro) | virtio, network, queue, descriptor, event-idx, interrupt, mmio |
| [io_uring 入门](notes/io-uring-intro) | linux, io, async, kernel, syscall |
| [smoltcp 接入与 axnet 本地化决策](notes/axnet-localization-decision) | smoltcp, axnet, network, dependency, capability, polling-fallback, device-mask |
| [一些问题](notes/实习问题) |  |
| [内存序：QEMU 掩盖的真板陷阱](notes/memory-ordering-smp) | rust, memory-ordering, riscv, smp, atomic, optimization |
| [异步 RX 侧 NAPI 状态机与 spawn 任务](notes/async-rx-napi-and-spawn) | rust, async, napi, uart, task |
| [异步 TX 侧四阶段 drain 与 fast retry](notes/async-tx-copier-drain) | rust, async, uart, drain, flush |
| [异步 UART 性能测试设计与结果](notes/async-uart-benchmark-design) | benchmark, performance, uart, latency, throughput, testing |
| [异步 UART 驱动总体架构](notes/async-uart-driver-architecture) | rust, async, uart, driver, ring-buffer, spsc, io-uring |
| [异步串口入口与全局实例](notes/async-uart-entry) | rust, async, os, riscv, uart |
| [异步驱动的泛型与 Trait 抽象](notes/async-driver-generics) | rust, generic, trait, async, uart |
| [异步驱动的线程安全](notes/async-driver-thread-safety) | rust, send, sync, atomic, thread-safety, uart |
<!-- NOTES_INDEX_END -->

> 新增内容：在 `docs/weeks/`、`docs/months/` 或 `docs/notes/` 下新建 md 文件，push 后自动更新此页。
