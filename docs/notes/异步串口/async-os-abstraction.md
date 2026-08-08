# OS 抽象具体实现

**日期**：2026-07-04
**标签**：rust, os, abstraction, trait, async

> 来源：StarryOS `crates/uart_16550/src/os/mod.rs`、`kernel/src/drivers/os_arceos.rs`、`openspec/specs/architecture/spec.md:352-388`、`openspec/specs/learned/spec.md:226-228,657,188-200,765`。
> 范围：2 trait 最小化设计、ArceOS 适配、跨平台扩展、与 DWMAC HAL 对比、AtomicWaker vs OsWakerSet。

## 答案

OS 抽象层只暴露 `OsRuntime` + `OsWakerSet`。从 5 trait 缩减到 2 trait（ADR-036），删了未被 driver 调用的 3 个。

## 设计动机：5→2 演化

ADR-035（2026-06-17）最初定义 5 个 OS 抽象 trait。包括 `OsRuntime`、`OsIrq`、`OsMmio`、`OsSpinNoIrq`、`OsWakerSet`。号称「最小完备接口集」。

ADR-036 后续审计发现：`OsIrq`、`OsMmio`、`OsSpinNoIrq` 三个 trait **从未被 driver 代码调用**。IRQ 注册、MMIO 映射、锁获取都在 OS 适配层处理。

YAGNI 原则：删除未使用 trait。tombstone 见 `openspec/specs/architecture/spec.md:352-388`。

## trait 完整定义

```rust
// crates/uart_16550/src/os/mod.rs:21-38
pub trait OsRuntime {
    fn spawn<F>(future: F, name: &str)
    where
        F: Future + Send + 'static,
        F::Output: Send;

    fn block_on<F>(future: F) -> F::Output
    where
        F: Future;
}

// crates/uart_16550/src/os/mod.rs:49-60
pub trait OsWakerSet: Send + Sync {
    fn new() -> Self;
    fn register(&self, waker: &Waker);
    fn wake(&self) -> u32;
}
```

两个 trait 涵盖的职责：

| Trait | 职责 | 驱动调用场景 |
|---|---|---|
| `OsRuntime` | 启动后台 task + 阻塞等 future | 启动 rx_copier、tx_copier；syscall 阻塞 read/write |
| `OsWakerSet` | 多等待者 waker 集合 | RX/TX/DRAIN 三个 waker；ring buffer 的 poll |

## ArceOS 适配

```rust
// kernel/src/drivers/os_arceos.rs:18-39
impl OsRuntime for ArceOsRuntime {
    fn spawn<F>(future: F, name: &str)
    where
        F: Future + Send + 'static,
        F::Output: Send,
    {
        let name = name.to_string();
        axtask::spawn_with_name(
            move || {
                axtask::future::block_on(future);
            },
            name,
        );
    }

    fn block_on<F>(future: F) -> F::Output
    where
        F: Future,
    {
        axtask::future::block_on(future)
    }
}
```

```rust
// kernel/src/drivers/os_arceos.rs:48-61
impl OsWakerSet for ArceOsWakerSet {
    fn new() -> Self {
        Self {
            inner: axpoll::PollSet::new(),
        }
    }

    fn register(&self, waker: &Waker) {
        self.inner.register(waker);
    }

    fn wake(&self) -> u32 {
        self.inner.wake() as u32
    }
}
```

## 选型理由

| 选择 | 理由 |
|---|---|
| `axtask::spawn_with_name` | axtask 是 ArceOS 内核 task 抽象，spawn 出独立 task |
| `axtask::future::block_on` | axtask 自带 future 阻塞执行器，复用不引 embassy-executor |
| `axpoll::PollSet` | axpoll 提供 PollSet，register/wake 是标准多等待者模式 |
| `move \|\|` 闭包 | future move 进闭包，闭包调 `block_on` 跑该 future |

`Send + 'static` 约束确保 future 能跨 task 边界 move。

## 跨平台扩展接口

`OsRuntime` 与 `OsWakerSet` 是公开 trait。其他 OS 可写适配：

```rust
// 假设为 Linux 裸机写适配
struct LinuxRuntime;
impl OsRuntime for LinuxRuntime {
    fn spawn<F>(future: F, name: &str) where F: Future + Send + 'static, F::Output: Send {
        // 例如用 tokio::spawn 替代 axtask
    }
    fn block_on<F>(future: F) -> F::Output where F: Future {
        futures::executor::block_on(future)
    }
}

struct LinuxWakerSet { /* ... */ }
impl OsWakerSet for LinuxWakerSet {
    fn new() -> Self { /* ... */ }
    fn register(&self, waker: &Waker) { /* ... */ }
    fn wake(&self) -> u32 { /* ... */ }
}
```

实现后 `AsyncUartDriver<R=LinuxRuntime, W=LinuxWakerSet, U>` 即可在 Linux 上跑。`R` 与 `W` 是泛型参数，由适配层注入。

## 与其他抽象的对比

`learned/spec.md:657` 记录：DWMAC HAL trait 7 个方法（`dma_alloc`、`dma_dealloc`、`mmio_phys_to_virt`、`mmio_virt_to_phys`、`wait_until`、`configure_platform`、`cache_flush_range`）。

我们 `UartPort`（4 方法）+ `OsRuntime`/`OsWakerSet`（2 trait，共 4 方法）= 6 接口。比 DWMAC HAL 7 方法**更精简**，正交性更好（ADR-036 已印证）。

正交性解释：
- `UartPort` 解决硬件访问（与具体 UART 型号有关）
- `OsRuntime` 解决 task 抽象（与具体 OS 有关）
- `OsWakerSet` 解决 waker 集合（与具体 OS 有关）

DWMAC 把硬件访问与平台依赖（DMA、MMIO 转换、cache flush）混在一个 trait 里，违反单一职责。

## AtomicWaker vs OsWakerSet

`learned/spec.md:226` 提到：`AtomicWaker` 需要 `critical-section` crate v1.0 的 `_critical_section_1_0_acquire/release` 符号。在 `lib.rs` 中用 `disable_irqs/enable_irqs` 实现。

```
ISR 上下文 → AtomicWaker::wake → 跨任务边界
            ↓
        critical-section 提供的 acquire/release
            ↓
        disable_irqs / enable_irqs
```

`AtomicWaker` 是单槽（`embassy_sync::waitqueue::AtomicWaker`），适合 ISR↔copier 唯一注册者场景。

`OsWakerSet::register/wake` 是多等待者容器（`axpoll::PollSet`），适合多消费者场景（epoll 多个等待者）。

两者**不能互相替代**。驱动用 `AtomicWaker` 处理 ISR↔copier 唯一通道；用 `OsWakerSet` 处理 copier↔消费者多等待者场景。

## 易错点

| 误判 | 真相 |
|---|---|
| trait 越多越灵活 | 越多越难跨平台；YAGNI 原则 |
| `OsRuntime::spawn` 启协程 | 启的是内核 task，独立栈 |
| `OsWakerSet` 等同 `AtomicWaker` | 多等待者 vs 单槽，不同场景 |
| driver 该知道 `axtask` / `axpoll` | driver 只知道 trait，适配层桥接 |
| 改 trait 就能改 OS 行为 | trait 是契约，OS 行为靠适配层 |
| 5 trait 比 2 trait 更完备 | 完备 = 满足需求，未用的 trait 是负担 |

## 经验

- 抽象层的设计标尺是「驱动真正调用的方法」
- YAGNI 原则：未用的 trait 是负担
- 跨平台靠 trait 抽象，OS 行为靠适配层注入
- 单槽 waker 与多等待者 waker 是不同场景的两种工具
- trait 是契约，方法是接口，调用方按契约写

## 问题解答

### 问题 1：RTOS 适配需要什么新 trait？

`axtask::future::block_on` 假设「当前 task 能阻塞等 future」。RTIC 等 RTOS 任务不能阻塞（协作式或事件驱动），需要换思路。

可能新增 trait：
- `OsTime::now()`：当前时间（用于超时）
- `OsSpawn::spawn(future, name)`：RTIC 风格的 spawn（无 block_on）
- `OsWfi::wait_for(event)`：等待事件（替代 block_on）

但「不能 block_on」对 UART 驱动影响很大。syscall 层大量用 `block_on(poll_io(...))` 阻塞 read/write。RTIC 适配下 syscall 阻塞路径全要改。

实际结论：RTIC 等不能 block_on 的 RTOS 不适合套用这个 trait 集。`axtask` 是「支持 block_on 的最小 OS」，`axtask::future` 是「最小 async 运行时」。换 OS 意味着换 syscall 阻塞实现，不是简单加 trait。

### 问题 2：`OsWakerSet::wake` 返回 `u32`（被 wake 的 waker 数），为什么签名要 `as u32` 强转？

`axpoll::PollSet::wake` 返回 `usize`（被 wake 的 waker 数）。trait 签名固定 `u32` 是设计选择：

- `u32` 足够：实际 waker 数不会超过 4G
- `u32` 跨平台稳定：与平台字长无关
- 适配层 `as u32` 强转在 32 位/64 位平台都安全

如果用 `usize`，trait 跨平台时会暴露平台字长差异。`u32` 是「足够表达 + 跨平台稳定」的折中。

`wake == 0` 的语义：没有 waker 注册时返回 0，调用方据此判断「无人等待」（flush 在 recheck 阶段可见这种状态）。