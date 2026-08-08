# 异步 RX 侧 NAPI 状态机与 spawn 任务

**日期**：2026-07-04
**标签**：rust, async, napi, uart, task

> 来源：StarryOS `crates/uart_16550/src/async_/driver.rs:209-255`、`crates/uart_16550/src/async_/ring_buffer.rs:93-104`、`crates/uart_16550/src/async_/isr.rs`、`kernel/src/drivers/os_arceos.rs:18-39`、`crates/uart_16550/src/os/mod.rs`。
> 范围：异步 RX 侧的批量抽干策略、copier-copier 与 copier-consumer 两级 waker、`axtask::spawn` 的真实含义。

## 答案

- ISR 关中断 + wake，copier 抽干 FIFO，空了才重开中断。
- ISR↔copier 用单槽 `AtomicWaker`，copier↔消费者用多等待 `OsWakerSet`。
- `R::spawn` 在内核里新建一个有独立栈的可调度 task，把 future 当 task 入口跑。

## NAPI 状态机

`rx_copier_loop` 用一个 `consecutive` 计数在两种模式间切换：

| 模式 | 条件 | batch | 是否每轮重开 RX 中断 |
|---|---|---|---|
| 中断模式 | `consecutive < 16` | 1024 | 是 |
| 轮询模式 | `consecutive >= 16` | 64 | 否 |

退出轮询：`receive_bytes` 返回 0 → `consecutive = 0` + 重开 RX 中断 + Pending。

每次 poll 的收尾：

- 读到字节 → `Poll::Ready(total)`，外层 loop 立刻再读。
- 读到 0 → `Poll::Pending`，任务让出。

抽干在同一次 poll 内完成。空转只烧在 copier 自己内部，不会饿死别的 task。

## 两级 waker 流水线

```
硬件 FIFO
  ↓ (ISR 关中断 + wake)
RX_WAKER (单槽 AtomicWaker)
  ↓ (copier 抽干 + push_batch)
rx.poll (OsWakerSet, 多等待)
  ↓ (wake)
消费者 (TtyRead / epoll)
```

两个 waker 不能合并，原因：

- `RX_WAKER` 只允许一个注册者（copier）。强行合并会让 ISR 触发一次唤醒多个 copier 副本。
- `rx.poll` 面向多消费者（epoll 可有多个等待者）。`AtomicWaker` 单槽语义装不下。

合并即破坏 SPSC 假设。

## 环形缓冲的 SPSC 约束

`ring_buffer.rs:30-44` 用 `UnsafeCell<Writer>` 与 `UnsafeCell<Reader>`，靠底层 `embassy_hal_internal` 的原子 Acquire/Release 维护 head/tail。

- 只有 copier 调 `push_batch`（单生产者）。
- 只有一个消费者调 `pop`（单消费者）。

多写多读会立刻变数据竞争。

## spawn 任务是什么

本仓库调用形态：`R::spawn(future, name)`（`os/mod.rs` 的 `OsRuntime::spawn`）。

实现路径（`kernel/src/drivers/os_arceos.rs:18-31`）：

1. `R::spawn` 调 `axtask::spawn_with_name(closure, name)`。
2. `axtask` 在内核里新建一个 task：独立栈、有名字、入就绪队列。
3. 调度器挑中该 task 后，闭包里执行 `axtask::future::block_on(future)`，把 Rust `Future` 跑在该 task 内。

一句话：`spawn` = 新建一个内核 task，把 future 当入口跑。

与 `tokio::spawn` 的差异：

| 维度 | `tokio::spawn` | `axtask::spawn` |
|---|---|---|
| 栈 | 无（future 借用所在线程栈） | 有（独立栈） |
| 调度 | 协作式（运行时 task 队列） | 内核调度器 |
| 等价物 | Go 的 goroutine | 内核线程式 task |

## OsRuntime 契约

`F: Future + Send + 'static`、`F::Output: Send` 的硬约束含义：

- `Send + 'static`：future 不能借用栈数据，必须能跨 task 边界 move。
- `Output: Send`：返回值能跨 task 传递。

违背 → `axtask::spawn_with_name` 直接编译失败。

## 易混点

**Ready 不等于读完。** Ready 是"这轮有进展，外层 loop 立刻再读"。Pending 才是真正的让出。

**轮询模式不烧别的 task。** 同一次 poll 内连续读到底，调度点仍在该 task。空转不出 task 边界。

**低 baud 几乎不进入轮询。** 115200 下约 87µs 一字节，CPU 抽得比字节来得快。`consecutive` 难爬到 16。NAPI 主要在高 baud 或突发时生效。这条直接连到 Q20/Q21 的高波特率与 DMA 决策。

**ISR 只 wake copier，不 wake 消费者。** 消费者只能由 copier 间接唤醒。ISR 直接 wake 消费者会让数据未入 ring 即被读，破坏 SPSC。

**单槽 AtomicWaker 与多等待 OsWakerSet 不是同一类东西。** 前者是 embassy 提供的单注册者 waker；后者是 OS 抽象层自定义的多等待容器。

## 经验

- 三件套可复用：电平型中断设备 + ISR 不能 await + 慢搬运 → ISR 关中断+wake、后台任务抽干、空了重开。
- 单 waker vs 多 waker 看「注册者数」。注册者多于 1 → 用 `OsWakerSet`；只能唯一注册者 → 用 `AtomicWaker`。
- 看 spawn 时分两层：OS 抽象 `R::spawn`（能力） vs 具体实现 `axtask::spawn_with_name`（机制）。机制可换，能力稳定。
- 看到 `Send + 'static` 立刻想：数据必须 move 进 future，不能持栈借用。
- NAPI 不是灵丹妙药，先看吞吐与到达间隔。低速场景几乎失效。