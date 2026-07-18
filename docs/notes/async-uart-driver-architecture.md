# 异步 UART 驱动总体架构

**标签**：rust, async, uart, driver, ring-buffer, spsc, io-uring

> 来源：StarryOS `crates/uart_16550/src/async_/`（`driver.rs`、`ring_buffer.rs`、`isr.rs`、`device_ops.rs`）。
> 驱动已基本定型，后续主要是性能优化和小修改。文档记录环形缓冲区、copier 任务、ISR→waker 机制的设计与算法，不涉及 OS 适配层细节。

## 数据流全景

驱动围绕两个无锁 SPSC 环形缓冲区组织，两个后台 copier 任务作为缓冲区与 UART 硬件之间的桥梁：

```
用户层                          内核/驱动层                        硬件

AsyncUartWriter ──push──> RingBufTx ──pop_batch──> TX Copier ──send_bytes──> UART THR
     ▲                      │    ▲                       │                      │
     │                 register_waker              TX_WAKER.wake()         THRE 中断
     │                      │                         │                      │
     │                      ▼                         ▼                      ▼
     │               [OsWakerSet poll]           [AtomicWaker]          [ISR 读取]
     │
AsyncUartReader <──pop── RingBufRx <──push_batch── RX Copier <──receive_bytes── UART RBR
                              ▲                       │                      │
                         register_waker          RX_WAKER.wake()         DR 中断
                              │                       │
                        [OsWakerSet poll]        [AtomicWaker]
```

方向相反，结构对称。差别在 TX 侧多了一层 drain 等待和慢轮询回退。

## 环形缓冲区

`RingBufTx` 和 `RingBufRx` 是对 `embassy_hal_internal::atomic_ring_buffer::RingBuffer` 的薄封装，各自持有 `UnsafeCell<Writer>` 和 `UnsafeCell<Reader>`。

SPSC 契约：
- RX：唯一生产者是 RX Copier，唯一消费者是 `AsyncUartReader`
- TX：唯一生产者是 `AsyncUartWriter`，唯一消费者是 TX Copier

每次 push / pop 成功后自动调用内部 `OsWakerSet::wake()` 通知对端。这保证了生产者写入后消费者被唤醒，消费者腾出空间后生产者被唤醒——无锁，无 syscall。

关键方法：

| 方法 | RX | TX |
|------|----|----|
| push / push_batch | RX Copier 写入 | 用户 writer 写入 |
| pop / pop_batch | 用户 reader 读取 | TX Copier 读取 |
| readiness 探测 | `occupied_len()` / `has_data()` | `vacant_len()` / `has_space()` |
| waker 注册 | `register_readable_waker()` | `register_writable_waker()` |

ring buffer 存储（64KB × 2）在内核初始化时分配为 `static mut`，由 `&'static RingBuffer` 引用传入，生命周期贯穿整个驱动运行期。

## Copier 任务

两个后台协程在 `OsRuntime::spawn()` 后以 `poll_fn` 循环运行。

### RX Copier：NAPI 风格中断合并

```
loop {
    if UART 有数据:
        读硬件 FIFO → push_batch 到 RingBufRx
        连续成功 NAPI_THRESHOLD 次 → 继续轮询（不重新启用中断）
    else:
        重新启用 IER::DATA_READY 中断
        返回 Poll::Pending（等待下次中断唤醒）
}
```

思路：数据密集时连续轮询比反复进出中断更高效；空闲时回到中断等待，不空转 CPU。NAPI 阈值设为 16 次，批量大小 64 字节。

### TX Copier：分层回退

```
loop {
    从 RingBufTx pop_batch → 尝试 send_bytes 到硬件 FIFO
    if 成功: 继续，fast retry 计数器归零
    else:
        fast retry 计数器++
        if 未耗尽（< 32 次）: 直接重试
        elif slow-poll 未耗尽: spin_loop 256 次 × 4096 轮 → 重试
        elif yield 未耗尽: yield_now 自我唤醒 4 次 → 重试
        else: 重新启用 THRE 中断，返回 Pending（纯 ISR 等待）
}
```

fast retry 应对 FIFO 瞬时满（发送者快于硬件）。slow-poll 给 FIFO 排空时间。yield 让出 CPU 后再试。最终回退到 ISR 等待，避免空转。

D1 真板数据：`slow_poll_exh=0`，`yield_exh=0`，说明本轮 slow-poll 100% 成功，未触发 yield 或纯 ISR 回退。这个路径是为 D1 THRE 边沿丢失问题设计的保险机制。

## ISR → Waker 机制

中断处理极简，只做三件事：

1. 读 ISR / IIR 判断中断类型
2. 禁用对应 IER 位（`IER::DATA_READY` 或 `IER::THR_EMPTY`）
3. `AtomicWaker::wake()` 通知对应 copier

三个全局 waker：`RX_WAKER`、`TX_WAKER`、`DRAIN_WAKER`。其中 `DRAIN_WAKER` 仅在 LSR TEMT（发送移位寄存器排空）时唤醒，服务于 `tcdrain` 等待者。

ISR 不做任何数据搬运——读硬件 FIFO、写 ring buffer 全在 copier 任务里完成。这避免了中断上下文中的锁竞争和长时间阻塞。

## Writer / Reader 的 unsafe 契约

`AsyncUartWriter::new()` 和 `AsyncUartReader::new()` 都是 `unsafe fn`。原因：

- 每个驱动实例只允许一个 writer 和一个 reader
- 多个 writer 同时 push 到同一个 SPSC ring buffer 会破坏数据完整性
- 多个 reader 同时 pop 会导致数据被不同消费者瓜分

内核层通过 `Arc<SpinNoPreempt<Writer>>` 在 clone 时共享同一个 writer，锁不跨 `.await` 点，避免死锁。这是"SPSC 物理约束 + 上层串行化"的设计——驱动层保证正确性，上层负责调度策略。

## 与 io_uring 的对比与取舍

设计中有多处与 io_uring 思路相似的地方，但也有明确不同的取舍：

### 相似点

| 概念 | io_uring | 本驱动 |
|------|----------|--------|
| 提交/执行分离 | SQ 入队 → 内核异步执行 | ring buffer push → copier 异步发送 |
| 完成通知 | CQ 条目 | `flush()` 轮询 `TxCompletion` + `DRAIN_WAKER` |
| 中断合并 | `IORING_SETUP_IOPOLL` 强制轮询 | NAPI：阈值触发后切换到轮询 |
| 无锁环形队列 | SQ/CQ 共享内存 | SPSC ring buffer（embassy） |
| 背压 | SQ 满返回 `-EAGAIN` | `vacant_len() == 0` → 注册 waker 等待 |

### 不同取舍

**不追求零拷贝**。io_uring 的一大卖点是用户态 buffer 直通内核，避免 `copy_from_user`。本驱动的 ring buffer 在内核空间，用户态 `write()` 数据经由 syscall 拷贝进 ring buffer，再由 copier 搬到硬件 FIFO。对 115200 bps 的串口来说，拷贝开销远小于线速限制，零拷贝无收益。

**不追求批量提交**。io_uring 的 `SQE` 可以一次提交多个 IO 请求。本驱动的 `try_write()` 是单次 push，但 copier 按 `pop_batch(COPIER_BUF_SIZE=1024)` 批量搬运到硬件。批量发生在驱动内部，不需要用户态关心。

**不做中断彻底关闭**。io_uring 的 `IORING_SETUP_SQPOLL` 让内核线程持续轮询 SQ，彻底不触发中断。本驱动保留中断作为最终回退——RX 空闲时靠中断唤醒，TX 慢轮询耗尽后也回退到 ISR。因为 UART 是低频设备（115200 bps ≈ 每秒最多 14400 字节），持续轮询的 CPU 开销不划算。

**不引入用户态 completion queue**。D1 真板评估后决定不做。现有 TX ring + copier 架构已经覆盖提交/执行分离，115200 bps 线速下用户态 CQ 无可见吞吐收益。保留为远期选项。

**SPSC 而非 MPSC**。io_uring 天然支持多线程提交到同一个 SQ。本驱动明确选择 SPSC——每个 UART 只有一个 writer。多生产者场景（kernel log + shell echo + 用户 write）在上层通过 `Arc<SpinNoPreempt<Writer>>` 串行化，不把并发复杂度下沉到驱动层。这样做的好处是 ring buffer 不需要原子 CAS 竞争，copier 不需要处理多生产者交错，代码更简单、更容易验证正确性。代价是上层需要一把锁——但这把锁不跨等待点，临界区短到只覆盖 ring buffer push，不是瓶颈。

## 参考

- [io_uring 入门](io-uring-intro.md)：同一笔记目录下的 io_uring 原理
