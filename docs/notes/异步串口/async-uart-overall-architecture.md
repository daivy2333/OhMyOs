# 异步串口总体架构：从硬件中断到 read 返回

**日期**：2026-08-21
**标签**：rust, async, uart, driver, interrupt, ring-buffer, spsc, tty, waker, napi

> 把整个异步串口子系统从硬件到 syscall 串一遍。驱动代码在 StarryOS [`crates/uart_16550`](https://github.com/daivy2333/StarryOS/tree/net-k3/crates/uart_16550)，OS 适配与入口在 [`kernel/src/drivers`](https://github.com/daivy2333/StarryOS/tree/net-k3/kernel/src/drivers)。

## 一句话架构

UART 收发由**两个无锁 SPSC 环形缓冲区**承载，**两个后台 copier 任务**在缓冲区与硬件之间搬数据，**ISR 只做"读状态、关中断、唤醒"**，向上经 TTY 行规程接到用户的 read/write。空闲时全链路睡觉，只有数据到达或空间腾出才被唤醒；数据密集时 copier 切到轮询模式连续抽干，把中断次数压到最低。

RX 与 TX 方向相反、结构对称。差别在 TX 多一层 drain 等待（tcdrain）和慢轮询回退。

## 数据流全景

```
用户层                          内核/驱动层                        硬件

write() ─push─> RingBufTx ─pop_batch─> TX Copier ─send_bytes─> UART THR
   ▲                │    ▲                  │                       │
   │           register_waker           TX_WAKER.wake()         THRE 中断
   │                │                    │                       │
read() <─pop── RingBufRx <─push_batch── RX Copier <─receive_bytes─ UART RBR
                 ▲                       │                       │
            register_waker          RX_WAKER.wake()           DR 中断
                 │                    │                       │
```

## 分层导航

按调用关系从底层到上层逐层看。每层职责独立，层与层之间通过 trait 或固定函数契约连接。

### 1. 入口与全局实例

- [`kernel/src/drivers/uart_init.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/drivers/uart_init.rs)：创建全局 UART 实例与 IER 适配层。
- [`kernel/src/drivers/mod.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/drivers/mod.rs)：模块声明与类型别名折叠。

三个关键选择：

- **`SpinNoIrq`** 而非 `SpinNoPreempt` 或 `Mutex`：UART 在中断和任务上下文都被访问，`SpinNoPreempt` 防不了 ISR，`Mutex` 可能睡眠。`SpinNoIrq` 靠写 `sstatus.SIE` 关中断加自旋，中断安全且不睡眠。
- **`lazy_static!` + `unsafe { new_mmio }`**：UART 构造需要运行时 `phys_to_virt()`，`static` 满足不了；构造时只存地址指针，地址有效性交给 SAFETY 注释。
- **`ier_cache: AtomicU8`** 在硬件 `UART` 之上多包一层。所有 IER 修改集中走 `update_ier(set, clear)`，不需要"读硬件→改→写回"，也防止多处改寄存器造成不一致。
- **`UART_STRIDE = 1`** 是硬约束：NS16550 只占 8 字节寄存器（offset 0-7），stride=4 会越界读触发 LoadFault。

### 2. OS 抽象层：两个 trait

- [`crates/uart_16550/src/os/mod.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/uart_16550/src/os/mod.rs)：`OsRuntime` 与 `OsWakerSet` 两个 public trait。
- [`kernel/src/drivers/os_arceos.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/drivers/os_arceos.rs)：ArceOS 适配——`spawn` 走 `axtask::spawn_with_name` + `axtask::future::block_on`，`OsWakerSet` 走 `axpoll::PollSet`。

驱动不知道 `axtask`/`axpoll`，只按 trait 写。跨平台就是换适配层：

| Trait | 职责 | 驱动调用场景 |
|---|---|---|
| `OsRuntime` | 启动后台 task + 阻塞等 future | 启动 rx/tx copier；syscall 阻塞 read/write |
| `OsWakerSet` | 多等待者 waker 集合 | RX/TX/DRAIN 三个 waker；ring buffer 的 poll |

最初定义了 5 个 trait（含 `OsIrq`/`OsMmio`/`OsSpinNoIrq`），审计发现这 3 个从未被驱动代码调用——IRQ 注册、MMIO 映射、锁获取都在适配层处理，于是删掉。抽象层的标尺是"驱动真正调用的方法"，没用的 trait 是负担。

`OsWakerSet::wake` 返回 `u32` 而非 `usize`，避免跨平台暴露字长差异。

### 3. 驱动核心

驱动本体在 `crates/uart_16550/src/async_/`，用三个泛型参数抽象依赖：

```rust
AsyncUartDriver<R: OsRuntime, W: OsWakerSet, U: UartPort>
```

| 泛型 | 约束 | ArceOS 实际绑定 | 作用 |
|---|---|---|---|
| R | `OsRuntime` | `ArceOsRuntime` | spawn copier + block_on |
| W | `OsWakerSet` | `ArceOsWakerSet` | ring buffer 唤醒集 |
| U | `UartPort` | `ArceOsUartPort` | UART 硬件访问 |

选泛型而非 trait object：copier 高频调硬件方法，动态分发（vtable）的 ~5-10ns 会累积。`type ArceOsDriver = AsyncUartDriver<ArceOsRuntime, ArceOsWakerSet, ArceOsUartPort>` 在编译期折叠。`R` 不在字段里，用 `PhantomData<R>` 占位保留 Send/Sync 边界。

- [`crates/uart_16550/src/async_/ring_buffer.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/uart_16550/src/async_/ring_buffer.rs)：环形缓冲区。
- [`crates/uart_16550/src/async_/driver.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/uart_16550/src/async_/driver.rs)：两个 copier 任务、TX 四阶段 drain、NAPI 状态机。
- [`crates/uart_16550/src/async_/isr.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/uart_16550/src/async_/isr.rs)：ISR handler，极简 4 步。
- [`crates/uart_16550/src/async_/device_ops.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/uart_16550/src/async_/device_ops.rs)：`AsyncUartWriter/Reader` 的 `embedded_io_async` 实现与 flush。

**环形缓冲区**：SPSC 契约——RX 唯一生产者是 RX Copier、唯一消费者是 Reader；TX 唯一生产者是 Writer、唯一消费者是 TX Copier。每次 push/pop 成功自动唤醒对端，无锁、无 syscall。64KB × 2 在内核初始化时分配为 `static mut`，生命周期贯穿驱动全程。

**驱动为何是 unsafe 的**：`AsyncUartWriter::new()`/`AsyncUartReader::new()` 是 `unsafe fn`。每个实例只允许一个 writer 和一个 reader，多个并发 push/pop 会破坏数据完整性。内核层通过 `Arc<SpinNoPreempt<Writer>>` 在 clone 时共享同一 writer，锁不跨 `.await` 点。SPSC 物理约束在驱动层保证，并发调度策略交给上层。

### 4. TTY 桥接

- [`kernel/src/drivers/ntty_async.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/drivers/ntty_async.rs)：异步 UART 在 TTY 层的接入。
- [`kernel/src/pseudofs/dev/tty/terminal/ldisc.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/pseudofs/dev/tty/terminal/ldisc.rs)：line discipline 与 `ProcessMode` 三态。
- [`kernel/src/pseudofs/dev/tty/pty.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/pseudofs/dev/tty/pty.rs)：PTY 主从，复用同一个三态机制。

TTY 阻塞读的关键问题是"数据从哪来、该在哪个 waker 上等"。`ProcessMode` 三个变体回答这个问题：

| 变体 | 数据来源 | 含义 |
|---|---|---|
| `Manual` | 只在 read 调用时处理 | 简单 fallback，后台进程 Ctrl+C 失效 |
| `External(callback)` | 外部数据源 | spawn tty-reader 后台 task，callback 把 waker 挂到数据源 PollSet |
| `None(PollSet)` | PTY master | raw 通道，走 `SimpleReader` 不做完整行规程 |

异步 UART 用 `External`：callback 把 tty-reader 的 waker 注册到 UART RX ring buffer 的 PollSet。数据流：

```
UART RX 收到字节
  → RX copier push_batch 到 ring buffer → rx.poll.wake()
  → tty-reader task 醒来 → InputReader.poll() 走 line discipline
  → poll_rx.wake() → 用户 read() 拿到数据
```

`External` 解决 `Manual` 的死穴：没有独立 task 持续 poll，后台进程按 Ctrl+C 时信号字节躺在 ring buffer 里没人处理。

### 5. syscall 面

- [`kernel/src/syscall/fs/ctl.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/syscall/fs/ctl.rs)：`tcdrain`（ioctl TCSBRK 0x5409）在 syscall 层硬编码拦截。
- [`kernel/src/syscall/fs/io.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/syscall/fs/io.rs)：`fsync` 等通用路径。

`tcdrain` 没走 `Tty::ioctl` 抽象，而是在 `sys_ioctl` 里直接 `use crate::drivers::uart_init` 调 `driver.tx_completion()` + register-then-recheck。这是为了真板验证快速打通留下的架构债：`Tty::ioctl` 不处理 TCSBRK，且 `AsyncUartWriter::flush` 里复制了一份几乎逐字相同的实现。功能正常，但单一事实来源被打破。正确方向是 `Tty::ioctl(TCSBRK) → AsyncUartWriter::flush`，让任何 TTY 后端实现 `embedded_io_async::Write::flush` 即自动支持 tcdrain。

## 关键机制

### ISR 极简：关中断 + wake，不搬数据

- [`crates/uart_16550/src/async_/isr.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/uart_16550/src/async_/isr.rs)

ISR 只做四件事，全程约 1.5µs：

1. 读 ISR/IIR 判断中断类型（RDR/RTI = RX，THRE = TX；THRE 时顺带查 TEMT 决定是否 wake DRAIN）
2. 禁用对应 IER 位（`DATA_READY` 或 `THR_EMPTY`）
3. `AtomicWaker::wake()` 唤醒 copier
4. 立即返回

**为什么必须禁用中断**：NS16550 是电平触发。RX FIFO 还有数据时 `DATA_READY` 条件一直成立，不禁用对应 IER 位就会无限重入 interrupt storm。这是"关掉敲门声"——告诉硬件这次处理完了，下次新事件再来。

ISR 不碰数据也不拿锁。读寄存器用 `IsrRegisters` + `read_volatile` 绕过高级 API（高级 API 需要 `SpinNoIrq` 锁，ISR 里拿锁违反极简原则）。依赖注入用函数指针：uart_16550 crate 不知道 StarryOS 的 `UART_PORT`，`uart_init.rs` 传入两个闭包（禁 RX、禁 TX）完成解耦。

### 两级 waker：单槽 AtomicWaker + 多等待 OsWakerSet

| 通道 | Waker 类型 | 为什么 |
|---|---|---|
| ISR → copier | `embassy_sync::AtomicWaker`（单槽） | 只允许一个注册者（copier） |
| copier → 消费者 | `OsWakerSet`（多等待，`axpoll::PollSet`） | epoll 可有多个等待者 |

合并会破坏 SPSC：ISR 直接 wake 消费者，消费者可能在数据未入 ring 时就读。三个全局 waker 各管一个语义角色：RX（字节到达）、TX（THR 空）、DRAIN（tcdrain 完成）。

### RX：NAPI 风格中断合并

- [`crates/uart_16550/src/async_/driver.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/uart_16550/src/async_/driver.rs) 的 `rx_copier_loop`

```
中断模式: consecutive < 16, batch 1024, 每轮重开 RX 中断
轮询模式: consecutive >= 16, batch 64, 不重开中断（连续抽干）
退出轮询: receive_bytes 返回 0 → consecutive=0 → 重开中断 → Pending
```

数据密集时连续轮询比反复进出中断高效；空闲时回到中断等待，不空转 CPU。轮询只烧在 copier 自己内部，同一个 poll 内连续读到底，不跨 task 边界，不饿死其他任务。115200 bps 下约 87µs 一字节，CPU 抽得比字节来得快，`consecutive` 难爬到 16——NAPI 主要在高波特率或突发时生效。

### TX：fast retry + 四阶段 drain

- [`crates/uart_16550/src/async_/driver.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/uart_16550/src/async_/driver.rs) 的 `tx_copier_loop` 与 `TxCompletion`

发送侧 FIFO 瞬时满时（发送者快于硬件），fast retry 先自旋 32 次；仍满则 slow-poll（`spin_loop` 256×4096 轮），再失败则 `yield_now` 让出 CPU 4 次，最后回退到 "注册 `TX_WAKER` + 重开 THRE 中断 + Pending"。fast retry 应对 FIFO 抖动常态，比立即 sleep + 等中断 + 调度的代价低；最终回退 ISR 等待避免空转。

**四阶段 drain**（`tx_completion().is_drained()`）决定 `tcdrain`/`flush` 何时返回：

| 字段 | 含义 | 对应字节状态 |
|---|---|---|
| `ring_empty` | 应用侧无新字节 | [1]→[2] 没新字节 |
| `!copier_active` | copier 已决定 sleep | copier 不会从 ring 取新字节 |
| `staged_bytes == 0` | 无字节等进 FIFO | [3] 无字节 |
| `transmitter_empty` | LSR TEMT，移位寄存器排空 | [4] FIFO 与移位寄存器全空 |

四者全 AND 才叫 drained。**THRE ≠ TEMT**：THRE 只说明 FIFO 可写，TEMT 才说明最后一个字节已离开芯片到线缆——所以 tcdrain 必须等 TEMT，`flush` 用 register-then-recheck（先注册 waker 再复查，防 wake 发生在 register 之前）。

## 线程安全与内存序

**两道防线**：

- Send/Sync：`AsyncUartDriver` 有 `&'static U` + `UnsafeCell`，编译器不自动推导，需 `unsafe impl Send/Sync`。前提是 `U: Send + Sync`、`RingBufRx/Tx<W>: Send + Sync`、`PhantomData<R>` 满足——违反约束就编译失败。
- `AtomicWaker`：ISR 上下文安全唤醒，`wake()` O(1) 原子操作（约 50ns）。

**内存序**：QEMU 单 hart 下 `Relaxed` 够用，真板多核必须升级 `Acquire/Release`。升级原则：写端 store `Relaxed→Release`，读端 load `Relaxed→Acquire`，RMW `Relaxed→AcqRel`。相关升级点：`ier_cache`、`tx_copier_active`、`tx_staged_bytes`、`tx_completion`。`SpinNoIrq` 内部是 RMW 原子操作，隐含 Acquire/Release——走它路径的内存序自动正确，问题出在绕过它的手动 `Relaxed` 用法。真板症状：`staged_bytes` 漂移、flush 过早返回、tcdrain 不返回、偶发 panic。

## 与 io_uring 的对照

| 概念 | io_uring | 本驱动 |
|---|---|---|
| 提交/执行分离 | SQ 入队 → 内核异步执行 | ring buffer push → copier 异步发送 |
| 完成通知 | CQ 条目 | `flush()` 轮询 `TxCompletion` + `DRAIN_WAKER` |
| 中断合并 | `IOPOLL` 强制轮询 | NAPI：阈值后切轮询 |
| 背压 | SQ 满返回 `-EAGAIN` | `vacant_len() == 0` → 注册 waker 等待 |

明确不做的：零拷贝（115200 bps 下拷贝开销远小于线速限制）、用户态 completion queue（D1 评估后无收益）、彻底关中断的 SQPOLL（UART 是低频设备，持续轮询不划算）、MPSC（每个 UART 只有一个 writer，多生产者由上层 `Arc<SpinNoPreempt>` 串行化）。

## 性能基线

- **D1 真板 115200 bps**：drain-each 64B 96.7% 线速、1024B 98.8% 线速；async 与 polling Console 同步吞吐差异 0.6-3.9 个百分点。
- **async 的区分点**：提交与发送解耦。256B 入队 11,280 KB/s（只写 ring buffer 就返回），Console 同步写受限于 11.45 KB/s。
- **尾部延迟**：Console 单字节平均 0.106 ms，async 0.192 ms（async 要等 waker 调度）；async 在 payload ≥15B 时每组出现一次 24-27 ms 尾部延迟，Console 无此现象。
- **QEMU 数据**：不仿真物理线延迟，线速 >100% 是预期，只作软件路径证据，不作绝对性能依据。

## 参考

- [异步 UART 驱动总体架构](async-uart-driver-architecture.md)：环形缓冲区、copier、ISR→waker 完整数据流
- [入口与全局实例](async-uart-entry.md)：SpinNoIrq / lazy_static / IER / MMIO
- [OS 抽象具体实现](async-os-abstraction.md)：5→2 trait 演化、ArceOS 适配
- [异步 RX 侧 NAPI 与 spawn](async-rx-napi-and-spawn.md)：中断合并、两级 waker
- [异步 TX 四阶段 drain 与 fast retry](async-tx-copier-drain.md)：drain 细节、THRE/TEMT、真板差异
- [TTY 阻塞读与 ProcessMode 桥接](async-tty-processmode.md)：Manual/External/None 三态、PTY
- [VFS 接口与 flush 实现路径](async-vfs-flush-path.md)：TCSBRK 硬编码架构债分析
- [ISR 极简 4 步流程](isr-minimal-4-step.md)、[ISR 触发后禁用中断](isr-disable-level-trigger.md)：电平触发中断
- [内存序：QEMU 掩盖的真板陷阱](memory-ordering-smp.md)：Relaxed → Acquire/Release
- [异步 UART 性能测试设计](async-uart-benchmark-design.md)、[与 polling Console 对比](async-vs-console-performance.md)
