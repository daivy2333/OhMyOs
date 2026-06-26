# 异步驱动的线程安全

**日期**：2026-06-25
**标签**：rust, send, sync, atomic, thread-safety, uart

> 来源：第 2 站 Q&A + 第 3 站 AtomicWaker 部分。
> 范围：异步驱动跨线程安全性的两道防线。

## 两道防线

| 防线 | 作用 | 体现 |
|---|---|---|
| Send/Sync | 编译期检查所有权跨线程 | AsyncUartDriver 类型标记 |
| AtomicWaker | ISR 安全唤醒 | 3 个静态 waker |

## Send/Sync 是 marker trait

| Trait | 含义 | 自动 |
|---|---|---|
| Send | 所有权可跨线程 move | 大多数类型自动 |
| Sync | &T 可跨线程共享 | 大多数类型自动 |

关系：`T: Sync` 当且仅当 `&T: Send`。

## 哪些不是 Send/Sync

| 类型 | Send | Sync | 原因 |
|---|---|---|---|
| `Rc<T>` | ❌ | ❌ | 引用计数非原子 |
| `RefCell<T>` | ✅ | ❌ | 借用检查非线程安全 |
| `*mut T` | ❌ | ❌ | 裸指针无同步 |

## unsafe impl Send/Sync 的合理性

```rust
unsafe impl<R, W, U> Send for AsyncUartDriver<R, W, U> {}
unsafe impl<R, W, U> Sync for AsyncUartDriver<R, W, U> {}
```

为什么需要 unsafe impl？
- 内部有 `&'static U`（裸引用）
- 内部有 `UnsafeCell`（环缓冲）

为什么实际安全？
- `U: Send + Sync`（trait 约束）
- `RingBufRx/Tx<W>: Send + Sync`（unsafe impl）
- 原子类型天然 Send + Sync
- `PhantomData<R>: Send + Sync`

Send/Sync 是编译期检查，违反就编译失败。

## AtomicWaker 静态唤醒模式

`embassy_sync::waitqueue::AtomicWaker` 是 ISR 安全的唤醒类型。

```rust
pub static RX_WAKER: AtomicWaker = AtomicWaker::new();
pub static TX_WAKER: AtomicWaker = AtomicWaker::new();
pub static DRAIN_WAKER: AtomicWaker = AtomicWaker::new();
```

| 维度 | AtomicWaker | 通用 Waker |
|---|---|---|
| 位置 | 静态全局 | 任务本地 |
| ISR 唤醒 | `wake()` O(1) | 需查找 |
| 复杂度 | O(1) | O(log n) BTreeMap |
| 任务注册 | 任意任务 | 通过 `register` API |

## 为什么 3 个独立 waker

UART 是专用驱动，固定 3 个语义角色：
- RX：字节到达
- TX：THR 空
- DRAIN：tcdrain 完成

不需要动态注册/注销。

## 为什么不用 register_irq_waker

通用 `register_irq_waker` 用 BTreeMap<usize, PollSet>，每次唤醒需要查找（O(log n)）。

UART 只需固定 3 个 waker，AtomicWaker 更直接更高效（O17 反优化教训）。

## ISR 中如何唤醒

```rust
fn uart_isr_handler(...) {
    let isr = regs.read_isr();
    match isr.interrupt_type() {
        Some(ReceivedDataReady) => {
            fn_disable_rx();
            RX_WAKER.wake();  // O(1) 原子操作
        }
        ...
    }
}
```

`wake()` 是原子操作，~50ns。

## 任务中如何注册 waker

copier 任务的 poll_fn 中：

```rust
RX_WAKER.register(cx.waker());
```

`AtomicWaker.register` 接受 `&Waker`，保存供 ISR 调用。

## 经验

- Send/Sync 编译期杜绝数据竞争
- 内部 UnsafeCell 必须手动 unsafe impl
- AtomicWaker 适合固定少量 waker 场景
- 不要为了"通用"用 BTreeMap
- ISR 唤醒 O(1) 即可