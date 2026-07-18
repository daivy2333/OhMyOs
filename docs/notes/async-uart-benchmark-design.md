# 异步 UART 性能测试设计与结果

**标签**：benchmark, performance, uart, latency, throughput, testing

> 来源：StarryOS `docs/benchmark-report-async.md`（2026-07-13）、`tests/benchmark.c`。
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

## 不做的事情

### RX fixed payload

不在可控条件下测接收固定字节数。原因：需要在另一端（串口发送端）精确控制发送时机和数据量，而当前 D1 测试环境只有一根串口线、没有对端设备。QEMU 可以做（写 /dev/console 回环），但 QEMU 不仿真物理线延迟，RX 数据的到达时机和 burst 行为与真板差异太大，测试结果不能作为真板依据。

### SMP 正确性

所有测试在单核环境下完成。多核场景下的并发读写、跨 hart 的 flush/tcdrain、IER enable/disable 竞争需要专门的 SMP 测试环境（至少 2 核同时操作同一个 UART），当前 D1 是单核 C906，QEMU 默认也是单 hart。

### CPU 使用率

当前只有行为计数器（`hw_send_zero` 次数、slow-poll 是否耗尽），没有 CPU 时间或利用率数据。计数器可以回答"回退路径是否被触发、是否耗尽"，但不能回答"花费了多少 CPU 时间"。

## 参考

- 完整报告：[`docs/benchmark-report-async.md`](https://github.com/daivy2333/StarryOS/blob/7836240/docs/benchmark-report-async.md)
- 测试代码：[`tests/benchmark.c`](https://github.com/daivy2333/StarryOS/blob/7836240/tests/benchmark.c)
- Q20 证据：[`.claude/analysis/q20-evidence/`](https://github.com/daivy2333/StarryOS/tree/7836240/.claude/analysis/q20-evidence)
