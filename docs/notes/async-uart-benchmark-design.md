# 异步 UART 性能测试设计与结果

**标签**：benchmark, performance, uart, latency, throughput, testing

> 来源：StarryOS `docs/benchmark-report-async.md`（2026-07-22，含 Q31/Q32 CPU 效率补充）、`tests/benchmark.c`。
> 数据采集平台：QEMU（NS16550，无物理线延迟）+ D1 真板（DW APB UART，115200 bps）。

## 测试设计思路

性能测试不只是跑分。这个 benchmark 的设计目标：**把端到端延迟拆解为可独立观测的环节，每个 section 回答一个具体问题**。

115200 bps 线速的理论上限是 11.52 KB/s。所有吞吐数字以此为参考——超过 100% 的（QEMU）说明驱动本身比物理线快，接近 100% 的（D1）说明驱动不是瓶颈。

## 测试矩阵

### S10：drain-each baseline

```
for i in 0..100:
    write_full(fd, payload, size)   // 阻塞写完整 payload
    tcdrain(fd)                      // 等待硬件发送完成
```

**测什么**：用户视角下"写完一个包并确认硬件已发出"的端到端吞吐和尾部延迟。这是最接近真实应用场景的测试。

**D1 结果**：64B 达 96.7% 线速，1024B 达 98.8% 线速。说明驱动在各种 payload 尺寸下都接近物理极限，没有明显的协议开销或等待浪费。

### S11：no-drain 入队速度

```
reset_tx_debug_counter()
计时开始：
for i in 0..100:
    write_full(fd, payload, size)   // 不 drain，只看入队
计时结束
tcdrain(fd)                          // 计时外 drain，采集快照
```

**测什么**：把"写 ring buffer 的速度"和"硬件发送时间"分开。enqueue KB/s 反映驱动内部 ring buffer 的吞吐上限，final drain ms 反映硬件清空缓冲区需要的时间。

**关键发现**：D1 1024B enqueue 达 10310 KB/s（远超 11.52 KB/s 线速），说明 ring buffer push 不是瓶颈。瓶颈在物理线速——1024B × 100 次写入只需约 10ms 入队，但 final drain 需要 5588ms（≈ 102400 / 11520 × 1000）。

### S12：batch-drain 摊薄开销

```
for i in 0..100:
    write_full(fd, payload, size)
    if i % 8 == 7: tcdrain(fd)      // 每 8 次写才 drain 一次
tcdrain(fd)                          // 末尾补一次
```

**测什么**：批量提交能否减少 drain 调用次数，提升有效吞吐。写 8 次才确认一次，允许 copier 更连续地搬运数据。

**结果**：D1 64B batch-drain 从 96.7% 提升到 98.8% 线速。drain 调用本身有开销——减少 drain 频率对小包有可见收益。

### S13/S14：边界路径

- **S13 `writev` 4×64B**：验证分片 IO 路径（`writev` + ONLCR 换行映射）。两端各 1 次短写，说明 `writev` 在 ring buffer 接近满时会正确返回部分写入，调用方需要处理。
- **S14 小包矩阵（64B/128B/256B）**：小包尺寸变化对吞吐和 tail 的影响。D1 128B 时线速最低（95.2%），说明存在最优 payload 尺寸区间。

### S20/S21：延迟与 FIFO 边界

- **S20 单字节延迟**：loop 100 次 `write(1B) + tcdrain()`，每次计时。测最小 payload 下的同步完成延迟，通常不触发 FIFO 满路径。D1 P50 = 0.189ms，P99 = 0.221ms。
- **S21 FIFO boundary matrix**：对 1/15/16/17/31/32/33/48/49 字节各测 100 次。这些尺寸围绕 D1 的 16B FIFO 边界展开——测 payload 跨过 FIFO 边界时的延迟突变。D1 在 size≥15 时每组出现 1 次 24-27ms 级 tail（P99/P50 从 ~1 跳变到 ~16），这是 D1 FIFO 16B burst 行为的已知边界。

### S30：非阻塞语义验证

```
fd1 = open("/dev/console", O_NONBLOCK)
read(fd1, 16B)  → 预期 EAGAIN（空缓冲）

fd2 = open("/dev/console")
ioctl(fd2, FIONBIO, 1)
read(fd2, 16B)  → 预期 EAGAIN
```

**测什么**：非阻塞模式的两种入口（`open(O_NONBLOCK)` 和 `ioctl(FIONBIO)`）在空缓冲时是否都正确返回 `EAGAIN`。这是之前修过的 FIONBIO 传播 bug 的回归验证。QEMU 和 D1 均 PASS。

### S40：TX 计数器代理

虽然没有 CPU 使用率采集，但 S40 提供了行为计数器：每个 benchmark 结束后读取 TX copier 的内部计数器快照。

**关键指标**：

| 计数器 | D1 值 | 含义 |
|--------|-------|------|
| `hw_send_zero` | 12,228,983 | 调用 `send_bytes` 但发送了 0 字节（FIFO 满）|
| `no_progress_budget` | 17,861 | fast retry 耗尽次数 |
| `slow_poll_exh` | 0 | slow-poll 回退耗尽次数 |
| `yield_exh` | 0 | yield 重试耗尽次数 |

`hw_send_zero` 很高不等于 CPU 空转——它是 slow-poll 路径下频繁探测 TX FIFO 的观测结果。`slow_poll_exh=0` 和 `yield_exh=0` 表示回退未触发：本轮测试中 slow-poll 阶段始终成功，从未落到更重的回退路径。

### 启动阶段 ring buffer benchmark

在用户态 benchmark 之前，内核初始化后直接测驱动内部能力。绕过 syscall、调度、串口线速，纯粹测 ring buffer 和 copier 的数据搬运速度。

D1 结果：TX ring buffer write 1,155,388 KB/s，RX ring buffer read 8,303,062 KB/s。这些数字说明驱动内部队列的处理能力远高于物理线速——用户态吞吐以线速为准是硬件限制，不是驱动限制。

### S41：TX CPU Work（inst/byte）

在 `instret`（RISC-V 指令退休计数器）区间内完成 100 次 `write_full()` + `tcdrain()` 完整发送链。64B/256B/1024B 三种 payload 各测 5 轮，取 median。

**测什么**：每发送一字节平均消耗多少条 CPU 指令。`instret` 是 hart 全量计数器（含同 hart 上所有背景活动），不是 CPU 占用率百分比。但它能回答"这一千字节的数据搬运花了 CPU 多少力气"，在无法测占用率的环境下是最接近 CPU 开销的 proxy。

**关键发现**：

D1 Console 三条 payload 稳定在约 1100 inst/byte。同步路径 `write() → 查 THRE → 写 THR → 等 TEMT → 返回` 极短，中间不需要 copier 调度、ring buffer 操作、中断唤醒。

D1 async 64B/256B 约 32800 inst/byte，1024B 升至 44716。每字节比 Console 多花 30-40 倍的指令——async 的每项架构能力（ring push、copier 唤醒、ISR 处理、waker 注册、`tcdrain` 等 TEMT）在这个单字节发送的短路上都变成了一份指令开支。1024B 的推高来自 ring backpressure 期间 `tcdrain` 等待 TEMT 的 busy-loop。

QEMU 两组 instret 在 13500-14800 之间，差距小。虚拟 UART 无物理线速限制，qemu-system 自身开销主导，不作为硬件证据。

### S42：TX Compute Overlap

先关掉 UART 测纯计算 idle 基线。再在 64B×100 UART write 窗口内执行同样的 compute kernel，比较可执行的有效计算量，算 overlap efficiency。

**测什么**：`write()` 入队返回后，UART 后台发送期间，CPU 有多少比例的时间可用于执行计算任务。这是 async 架构"提交即返回"优势的量化——之前只能定性说"不阻塞"，现在有具体数字。

**关键发现**：

D1 async `write()` 约 1.6 ms 即返回（仅 ring enqueue），UART 后续用约 554 ms 物理发送全部数据。在这段发送空窗里，CPU 可执行 53.5% 的计算量。也就是说，`write()` 返回后 CPU 还有超过一半的时间能干别的事。

D1 Console `write()` 同步完成耗时约 554 ms——已超过 64B×100 的理论线时 542.5 ms，数据在 `write()` 返回前已发完。overlap=0 不是 bug，是 polling 模式下 `write()` 语义的必然结果：UART 不发送完就不可能返回。

### S43：Timer Wakeup Overshoot

`clock_nanosleep(TIMER_ABSTIME)` 按 5 ms 间隔绝对时间睡眠。idle 组纯睡眠，loaded 组在 4096B burst write 期间睡眠。各测 5 组，每组 50 次采样。

**测什么**：定时器唤醒的调度延迟。idle 组回答"无事时 5 ms 定时能不能准时"，loaded 组回答"UART 正在大量发送时定时器会不会被推迟"。

**关键发现**：

D1 idle P50：Console 8.42 ms，async 9.53 ms——两组接近，无事时唤醒精度相当。

D1 loaded：Console 为 `not-applicable`——同步 `write()` 用约 355 ms 发送 4096B，耗尽整个窗口，不存在"loaded 期间测唤醒"的场景。async loaded P50=25.78 ms，比 idle 的 9.53 ms 明显升高，来自发送积压期的唤醒干扰。

这里暴露了 Console 在 loaded 场景的结构性问题：不是测不到 loaded 数据，而是 loaded 场景本身不存在——只要开始发送，CPU 就被 `write()` 锁死了，没有重叠窗口去观测唤醒偏移。

## 不做的事情

### RX fixed payload

不在可控条件下测接收固定字节数。原因：需要在另一端（串口发送端）精确控制发送时机和数据量，而当前 D1 测试环境只有一根串口线、没有对端设备。QEMU 可以做（写 /dev/console 回环），但 QEMU 不仿真物理线延迟，RX 数据的到达时机和 burst 行为与真板差异太大，测试结果不能作为真板依据。

### SMP 正确性

所有测试在单核环境下完成。多核场景下的并发读写、跨 hart 的 flush/tcdrain、IER enable/disable 竞争需要专门的 SMP 测试环境（至少 2 核同时操作同一个 UART），当前 D1 是单核 C906，QEMU 默认也是单 hart。

### CPU 使用率

S41 用 `instret` 提供了 CPU work proxy（inst/byte），但这是 hart 全量指令计数，不是 CPU 占用率百分比。S40 的行为计数器可以回答"回退路径是否被触发、是否耗尽"，但不能回答"花费了多少 CPU 时间"。StarryOS 当前不支持 per-task CPU time accounting，无法采集真正的 CPU 占用率数据。

## 参考

- 完整报告：[`docs/benchmark-report-async.md`](https://github.com/daivy2333/StarryOS/blob/7836240/docs/benchmark-report-async.md)
- 测试代码：[`tests/benchmark.c`](https://github.com/daivy2333/StarryOS/blob/7836240/tests/benchmark.c)
- Q20 证据：[`.claude/analysis/q20-evidence/`](https://github.com/daivy2333/StarryOS/tree/7836240/.claude/analysis/q20-evidence)
