# Rust 异步驱动语言特性

**日期**：2026-06-25
**标签**：rust, async, generic, unsafe, smart-pointer, send, sync, deref

> 范围：异步串口项目用到的 Rust 语言特性
> 目的：下次回来能快速回忆

## 泛型

泛型 = 编译期多态。同一段代码支持多种类型，编译时为每个具体类型生成独立代码（单态化），零运行时开销。

```rust
fn largest<T: PartialOrd>(list: &[T]) -> &T { ... }

largest(&[1, 2, 3]);       // 生成 largest<i32>
largest(&["a", "b"]);      // 生成 largest<&str>
```

trait bound 是冒号后的约束，泛型代码能调用该 trait 的方法（编译期确认）。本项目示例：

```rust
pub struct AsyncUartDriver<R: OsRuntime, W: OsWakerSet, U: UartPort> { ... }
```

| 维度 | 泛型（静态） | trait object（动态） |
|---|---|---|
| 写法 | `T: Trait` | `dyn Trait` |
| 性能 | 零开销 | 每次调用 ~5-10ns vtable |
| 代码 | 每个类型一份 | 共享一份 |
| 灵活性 | 编译期固定 | 运行时可换 |

热路径选泛型。本项目 `AsyncUartDriver<R, W, U>` 用三泛型。

type alias 折叠复杂类型：

```rust
// uart_init.rs
pub type ArceOsDriver = AsyncUartDriver<ArceOsRuntime, ArceOsWakerSet, ArceOsUartPort>;
```

编译期纯替换，零开销。

PhantomData 保留"逻辑上有但实际无"的类型：

```rust
// driver.rs:129
_runtime: PhantomData<R>,
```

`R` 不出现在字段里（只在 spawn 时用 `R::spawn`），但仍是泛型参数。两作用：满足"未使用类型参数"检查；保留 Send/Sync 边界。

## unsafe

unsafe 解锁 5 种能力：

| 能力 | 例子 |
|---|---|
| 解引用裸指针 | `*const T` / `*mut T` |
| 调用 unsafe fn | `Uart16550::new_mmio(...)` |
| 访问 `static mut` | `static mut X: T` |
| 实现 unsafe trait | `unsafe impl Send for T` |
| 访问 union 字段 | `union { ... }` |

SAFETY 注释约定：任何 unsafe 代码块必须有 `// SAFETY:` 注释，说明这次 unsafe 为何安全。

```rust
// uart_init.rs:62
SpinNoIrq::new(unsafe {
    // SAFETY:
    // 1. get_uart_mmio_virt() 返回 axruntime 启动时建立的合法虚拟地址
    // 2. UART 在 SpinNoIrq 内，不会被并发访问
    // 3. stride=1 正确（NS16550 字节寻址）
    Uart16550::new_mmio(...)
});
```

本项目的 unsafe 用法：

| 位置 | 类型 | 原因 |
|---|---|---|
| `uart_init.rs:62` | unsafe 块 | new_mmio 接受 NonNull |
| `isr.rs:42` | unsafe fn | 读 MMIO |
| `ring_buffer.rs:79` | unsafe 块 | UnsafeCell 内部可变性 |
| `driver.rs:136` | unsafe impl | 编译器无法自动证明 Send/Sync |

## 智能指针

`Box<T>` 堆分配，单一所有者，drop 时释放。常用：dyn Trait 持有、递归类型。

```rust
// ntty_async.rs
process_mode: ProcessMode::External(Box::new(move |waker| {
    uart_init::driver().rx.poll.register(&waker);
}))
```

`Arc<T>` 原子引用计数，跨线程共享。

```rust
// device_ops.rs
pub struct AsyncUartReader<R, W, U> {
    driver: Arc<AsyncUartDriver<R, W, U>>,
}
```

| 类型 | 计数 | 线程 | 性能 |
|---|---|---|---|
| `Box<T>` | 单一所有者 | 取决于 T | 零开销 |
| `Rc<T>` | 引用计数 | ❌ 单线程 | 极快 |
| `Arc<T>` | 原子引用计数 | ✅ 跨线程 | clone/drop 几 ns |

`UnsafeCell` 内部可变性：

```rust
// ring_buffer.rs
pub struct RingBufRx<W: OsWakerSet> {
    writer: UnsafeCell<Writer<'static>>,
    reader: UnsafeCell<Reader<'static>>,
    pub poll: W,
}
```

`Writer` / `Reader` 方法要 `&mut self`，但 `RingBufRx` 的 API 用 `&self`。`UnsafeCell` 告诉编译器"我接管"。安全性由调用方保证（SPSC 约定：只有一个写者、一个读者）。

`Once<T>` 单次初始化：

```rust
// uart_init.rs
static DRIVER: Once<Arc<ArceOsDriver>> = Once::new();
DRIVER.call_once(|| driver);  // 只第一次执行
```

`lazy_static!` 内部用 `Once + Deref` 实现。

## Send / Sync

| Trait | 含义 | 自动 |
|---|---|---|
| `Send` | T 所有权可跨线程 move | 大多数类型自动 |
| `Sync` | `&T` 可跨线程共享 | 大多数类型自动 |

`T: Sync` 当且仅当 `&T: Send`。

非 Send/Sync：

| 类型 | Send | Sync | 原因 |
|---|---|---|---|
| `Rc<T>` | ❌ | ❌ | 引用计数非原子 |
| `RefCell<T>` | ✅ | ❌ | 借用检查非线程安全 |
| `*mut T` | ❌ | ❌ | 裸指针无同步 |

unsafe impl：

```rust
// driver.rs:136
unsafe impl<R, W, U> Send for AsyncUartDriver<R, W, U> {}
unsafe impl<R, W, U> Sync for AsyncUartDriver<R, W, U> {}
```

`AsyncUartDriver` 内部有 `&'static U`（裸引用）+ `UnsafeCell`，编译器无法自动证明。实际安全：`U: Send + Sync`（trait 约束）；`RingBufRx/Tx<W>: Send + Sync`（unsafe impl）；原子类型天然 Send + Sync。Send/Sync 是编译期检查，违反就编译失败。

## 引用与 Deref

| 类型 | 安全 | 可空 | 借用检查 |
|---|---|---|---|
| `&T` / `&mut T` | ✅ | ❌ | ✅ 编译期 |
| `*const T` / `*mut T` | ❌ | ✅ | ❌ |
| `NonNull<T>` | 编译期非空 | ❌ | ❌ |

`NonNull<u8>` 用于构造全局 UART，绕开借用检查。

Deref trait：

```rust
pub trait Deref {
    type Target: ?Sized;
    fn deref(&self) -> &Self::Target;
}
```

让自定义类型"表现得像" `&Target`。

Deref coercion 自动转换：

```rust
fn takes_str(s: &str) { ... }
let s = String::from("hi");
takes_str(&s);  // &String 自动转为 &str
```

本项目的 Deref 链——`uart_instance().lock()` 完整调用：

```
&lazy_static::UART          (1)
   ↓ Deref
&SpinNoIrq<Uart16550>       (2)
   ↓ .lock()
SpinNoIrqGuard<'_, Uart16550>
   ↓ Deref
&mut Uart16550              (3)
   ↓ .receive_bytes(buf)
```

3 次 Deref 全部自动，编译器完成。

Guard 模式：

```rust
// kspin 内部（伪代码）
impl<T> Drop for SpinNoIrqGuard<'_, T> {
    fn drop(&mut self) {
        // 恢复 sstatus.SIE
    }
}
```

Guard 实现 `Deref + DerefMut + Drop`：Deref 让你能调 T 的方法；Drop 自动释放锁。

RefCell 没用。本项目用 SpinNoIrq 代替 RefCell：

| | RefCell | SpinNoIrq |
|---|---|---|
| 借用检查 | 运行时 | 编译期 |
| 线程安全 | ❌ | ✅ 关中断 |
| 失败时 | panic | 永不失败（spin） |

`SpinNoIrq` 更适合 ISR + 任务上下文的互斥。