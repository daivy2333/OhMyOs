# 异步驱动的泛型与 Trait 抽象

**日期**：2026-06-25
**标签**：rust, generic, trait, async, uart

> 来源：StarryOS `uart_16550/src/async_/driver.rs:115-145` 通读。
> 范围：`AsyncUartDriver<R, W, U>` 三泛型结构。

## 三个泛型的角色

`AsyncUartDriver<R: OsRuntime, W: OsWakerSet, U: UartPort>`：

| 泛型 | 约束 | 实际绑定 | 作用 |
|---|---|---|---|
| R | OsRuntime | ArceOsRuntime | spawn copier + block_on |
| W | OsWakerSet | ArceOsWakerSet | ring buffer 唤醒集 |
| U | UartPort | ArceOsUartPort | UART 硬件访问 |

## 为什么是 3 个泛型

热路径零开销。copier 每纳秒都调 `receive_bytes` / `send_bytes`，动态分发开销累积显著。

| 维度 | 泛型（静态） | trait object（动态） |
|---|---|---|
| 性能 | 零开销 | 每次 ~5-10ns vtable |
| 代码 | 每个类型一份 | 共享一份 |
| 灵活性 | 编译期固定 | 运行时可换 |

## type alias 折叠

```rust
pub type ArceOsDriver = AsyncUartDriver<ArceOsRuntime, ArceOsWakerSet, ArceOsUartPort>;
```

`pub type` 是编译期纯替换，零运行时开销。

StarryOS 用 4 个 type alias 折叠所有泛型：`ArceOsDriver`、`ArceOsReader`、`ArceOsWriter`、`AsyncTty = Tty<ArceOsReader, ArceOsWriter>`。

## PhantomData 用途

`R` 不在字段里（只在 spawn 时用 `R::spawn`），但仍是泛型参数。

```rust
_runtime: PhantomData<R>,
```

两作用：
1. 满足"未使用类型参数"检查
2. 保留 Send/Sync 边界

`PhantomData<T>` 是 ZST（零大小类型），不占内存。

## R 出现 4 次

| 位置 | 作用 |
|---|---|
| 泛型参数声明 | 类型签名 |
| PhantomData marker | 逻辑拥有 |
| impl 块 trait bound | 方法可用 R |
| unsafe impl trait bound | Send/Sync 派生 |

多写 PhantomData 字段无意义（ZST 重复，编译器警告 unused field）。