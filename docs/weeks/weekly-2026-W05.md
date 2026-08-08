# W05 - Console polling 基线 + CPU 效率对比

**周期**：2026-07-19 ~ 2026-07-25

> 分支：`console-lichee`（Console 基线）→ `uart-lichee`（CPU 效率基准与收尾）
>
> 提交数：13 个

## 本周工作

### Console polling 基线完成

在 `console-lichee` 分支删除了 async UART（crate、driver、copier、IRQ），替换为 `ProcessMode::Polling` + `InputReader` 全功能 polling Console。

验证结果：
- QEMU：shell 交互正常，`/bin/benchmark` 完整运行
- D1 真板：64B 99.0%、256B 99.3%、1024B 99.4% 线速，`short_writes=0`，`drain_errors=0`

commit [`c36544b`](https://github.com/daivy2333/StarryOS/commit/c36544b)

### async vs Console 吞吐与延迟对比

对四组环境（QEMU/D1 × async/Console）做了 2×2 交叉对比。数据来源：[`docs/benchmark-report-async.md`](https://github.com/daivy2333/StarryOS/blob/uart-lichee/docs/benchmark-report-async.md)。

发现：

- **S10 drain-each**：两者在 D1 上都接近 115200 bps 上限（96.8%-99.4%），差异 0.6-3.9 个百分点
- **S11 提交速度**：async 256B 入队 11,280 KB/s，Console 同步写 11.45 KB/s——差距近 1000 倍。这是 `write()` 零等待 vs 阻塞 22ms 的架构差异
- **S20 单字节延迟**：Console 0.106 ms，async 0.192 ms。Console 的 polling drain 路径更短
- **S21 FIFO 边界**：async 在 payload >=15B 时每组出现一次 24-27ms tail（P99 骤升），Console P99 始终接近 P50。async 根因未定位

结论：115200 bps 下两者吞吐已无区分度。async 的价值在 `write()` 非阻塞返回、RX 支持、并发模型，不在吞吐。

### CPU 占用率测量设计

写了 Console 性能与测量设计分析，覆盖 TX/RX 调用链、CPU/idle/内存口径、IRQ-off 窗口、延迟与抖动、QEMU/D1 对照矩阵。当前 benchmark 无 CPU 使用率数据，S40 counter proxy 不能替代百分比。

commit [`73b8973`](https://github.com/daivy2333/StarryOS/commit/73b8973)

### 文档体系统一与清理

从 `console-lichee` 同步 OpenSpec 文档到 `uart-lichee`，然后清理 console 专属内容：

- 删除 `console-polling-baseline` change archive 和 capability spec
- SNAPSHOT 重写为 `uart-lichee` 上下文
- `console-performance-measurement-design.md` → Artifact-Archive
- `console-benchmark-qemu-d1.md` → 通用 `benchmark-qemu-d1.md`
- improvements 删 I11（polling Console），留 I12（benchmark 方法论）
- references R41 路径更新，R42 标记 ARCHIVED

commit [`506e78e`](https://github.com/daivy2333/StarryOS/commit/506e78e)（同步）、[`f8819a2`](https://github.com/daivy2333/StarryOS/commit/f8819a2)（清理）

### Q31 async UART CPU 效率基准

在 `benchmark.c` 中增加了三个 CPU 效率指标，用 instruction count 替代百分比来衡量 CPU 开销：

- **S41 inst/byte**：每发送一字节平均执行多少条指令。D1 真板 async 结果：32,818（64B）/ 32,792（256B）/ 44,716（1024B）。越小越省 CPU
- **S42 overlap**：CPU 与 UART 硬件并行工作的时间比例。async D1 为 0.54，说明约一半时间并行
- **S43 timer overshoot**：idle 状态下 9.5ms，loaded 状态下 25.8ms。衡量调度延迟

QEMU 同步采集但不作为硬件证据。D1 数据来自 `fullbench-command`，证据冻结于 `a9ce8a34` / `50a2a876`。

实现上修改了 `benchmark.c`（+751 行 S41/S42/S43 逻辑）、`time_math.rs`（D1 定时器精度补全）、`time.rs`（时钟源接入）。

commit [`7d44cb1`](https://github.com/daivy2333/StarryOS/commit/7d44cb1)

### Q32 Console CPU 效率基准同步

从 `console-lichee` 分支同步 Console polling 的 CPU 效率数据到 `uart-lichee`：

- 恢复 OpenSpec change（proposal、design、spec、tasks、iterations 000-002）
- 恢复冻结证据日志（`q32-console-cpu-efficiency-evidence/`，含 D1 和 QEMU 日志及 SHA256 哈希）
- 同步 `console-benchmark-qemu-d1.md` runbook
- 更新 SNAPSHOT 和 tasks 中的 Q31/Q32 完成记录

Console D1 关键数据：S41 inst/byte 1,194（64B）/ 1,105（256B）/ 1,106（1024B）；S42 overlap 0.00（polling 无并发）；S43 idle 8.4ms，loaded not-applicable。

commit [`9810000`](https://github.com/daivy2333/StarryOS/commit/9810000)（同步）、[`7ddb5b4`](https://github.com/daivy2333/StarryOS/commit/7ddb5b4)（runbook）

### CPU 效率交叉对比

在 `benchmark-report-async.md` 中增加 S41/S42/S43 对比章节：

- async 单字节指令开销是 Console 的 27-30 倍（32,818 vs 1,194 inst/byte）。async 每字节多了 IRQ 处理、copier 调度、waker 操作
- S42 overlap 0.54 vs 0.00：async 的 copier 让 CPU 在 UART 发送期间可执行其他任务。Console 的 polling drain 全程阻塞
- S43 loaded 25.8ms vs not-applicable：async 在高负载下调度延迟显著，但 Console 无法在此场景运行（polling 阻塞直到 drain 完成，无"loaded"状态）

QEMU 数据不作为硬件证据，仅用于功能验证。

commit [`e8822ad`](https://github.com/daivy2333/StarryOS/commit/e8822ad)（报告更新）、[`0d4edff`](https://github.com/daivy2333/StarryOS/commit/0d4edff)（日志同步）

### 归档与收尾

Q31 和 Q32 两项 change 归档，capability spec 同步到 `openspec/specs/`：

- `uart-cpu-efficiency-benchmark` spec：7 条需求
- `console-cpu-efficiency-benchmark` spec：10 条需求
- 新增 R42-R46 参考（Q31/Q32 spec、runbook、analysis、doc-sync checklist）

commit [`636b03b`](https://github.com/daivy2333/StarryOS/commit/636b03b)

## 下周工作

1. 其实异步串口的代码已经很久没有更改过了，最近的工作都相当于是在给d1写适配，也算是积累了不少真板调试的经验，当前最关心的就是另外一块板子能够早点到我手里，这样就能早点进行测试、优化、再验证，然后把异步串口的工作早点结束了。下周我会继续看看有什么优化空间。
2. 提前开始网卡驱动的探索，看看embassy，看看别人的工作，然后总结成一个文档供后续参考吧，这个难度更高一些，多参考参考应该有好处。

## 参考

- [异步 UART 性能测试设计与结果](../notes/异步串口/async-uart-benchmark-design.md)
- [DMA：绕过 CPU 的数据搬运](../notes/学习内容/dma-intro.md)
- [MMIO：用 load/store 指令操作硬件](../notes/学习内容/mmio-intro.md)
- [Async UART 与 polling Console 性能对比](../notes/异步串口/async-vs-console-performance.md)
- [性能对比报告](https://github.com/daivy2333/StarryOS/blob/uart-lichee/docs/benchmark-report-async.md)
- [Console QEMU 日志](https://github.com/daivy2333/StarryOS/blob/uart-lichee/docs/qemu_console.md)
- [Console D1 日志](https://github.com/daivy2333/StarryOS/blob/uart-lichee/docs/d1_console.md)
- [async QEMU 日志](https://github.com/daivy2333/StarryOS/blob/uart-lichee/docs/qemu_out.md)
- [async D1 日志](https://github.com/daivy2333/StarryOS/blob/uart-lichee/docs/d1_out.md)
