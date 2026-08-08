# 异步串口入口与全局实例

**日期**：2026-06-25
**标签**：rust, async, os, riscv, uart

> 来源：StarryOS `kernel/src/drivers/uart_init.rs:38-73` 通读。
> 范围：异步串口实现的入口层——模块声明、常量、全局实例。

## 模块入口

`mod.rs` 共 14 行：

```rust
pub mod ntty_async;
pub mod os_arceos;
pub mod uart_init;
pub use ntty_async::ASYNC_TTY;
pub type AsyncTty = Tty<ArceOsReader, ArceOsWriter>;
```

`AsyncTty` 把 `Tty<R, W>` 的两个泛型折叠为单一名字。编译期纯替换，零运行时开销。

## 三个常量

```rust
pub const UART_MMIO_BASE_PHYS: usize = 0x10000000;
pub const UART_STRIDE: u8 = 1;
const BUF_SIZE: usize = 64 * 1024;
```

- `UART_MMIO_BASE_PHYS`：RISC-V QEMU virt 平台 UART 物理地址。
- `UART_STRIDE = 1`：NS16550 寄存器仅 8 字节，stride=4 越界会触发 LoadFault。
- `BUF_SIZE = 64 KiB`：与 Pipe 一致，避免重新选型。

## 全局 UART 实例

```rust
lazy_static! {
    static ref UART: SpinNoIrq<Uart16550<MmioBackend>> = SpinNoIrq::new(unsafe {
        Uart16550::new_mmio(...)
    });
}
```

三个选择：

- **`SpinNoIrq`**：UART 在中断和任务上下文都被访问。`SpinNoPreempt` 不能防 ISR。`Mutex` 可能睡眠。`SpinNoIrq` 关中断+自旋符合需求。
- **`lazy_static!`**：UART 构造需要运行时 `phys_to_virt()`。`static` 要求编译期求值。
- **`unsafe { new_mmio }`**：构造时只存地址指针。编译器无法验证地址有效性，需 SAFETY 注释。

## IER 适配层

```rust
lazy_static! {
    static ref UART_PORT: ArceOsUartPort = ArceOsUartPort {
        uart: &UART,
        ier_cache: AtomicU8::new(0),
    };
}
```

`ArceOsUartPort` 在 `UART` 之上多封装 `ier_cache: AtomicU8`。

`ier_cache` 解决两件事：

1. `set_ier` 不再需要"读硬件 → 修改 → 写回"。
2. 集中所有 IER 修改路径，防止不一致。

## IER 寄存器

IER 是 NS16550 中断使能寄存器（offset 1），共 4 个有效位：

| bit | 名称 | 触发条件 |
|---|---|---|
| 0 | DATA_READY | RX FIFO 有数据可读 |
| 1 | THR_EMPTY | THR 可接受新字节 |
| 2 | RLS | 接收线路状态变化 |
| 3 | MS | 调制解调器状态变化 |

本项目使能 bit 0 + bit 1（RX + TX 中断）。Console 只使能 bit 0（TX 走 polling）。

`update_ier(set, clear)` 通过位运算做原子开关。

## SpinNoIrq 机制

`SpinNoIrq` 在 RISC-V 上的汇编流程：

```text
lock() 时：
  csrr   t0, sstatus        # 读 sstatus
  andi   t1, t0, (1<<1)     # 提取 SIE 位
  csrc   sstatus, (1<<1)    # 关闭 SIE
  保存 t1 到 guard 字段
  自旋等锁可用
  拿锁后返回 guard

drop guard 时：
  csrs   sstatus, t1        # 恢复 sstatus.SIE
```

`sstatus.SIE` 是 S-mode 全局中断使能位。关闭后所有外部中断不进入。

ISR 中调 `update_ier` 不会死锁：ISR 触发时硬件已自动关中断。`save+disable` 在 ISR 中冗余但无冲突。

## lazy_static 机制

宏展开后等价于：

```rust
struct UART { __private_field: () }
static UART: Once<Uart16550<MmioBackend>> = Once::new();

impl Deref for UART {
    type Target = Uart16550<MmioBackend>;
    fn deref(&self) -> &Self::Target {
        self.get_or_init(|| Uart16550::new_mmio(...))
    }
}
```

`Deref` 触发首次初始化。后续访问走 fast path。`Once::get_or_init` 保证只执行一次。RISC-V 单核下也用，因为存在递归调用 lazy_static 的可能。

## Bitflags 机制

`IER`、`LSR` 是 `bitflags!` 宏生成的类型：

```rust
IER::DATA_READY              // bit 0
IER::THR_EMPTY               // bit 1
IER::empty()                 // 空集
ier.contains(IER::DATA_READY) // 检查位
IER::from_bits_truncate(b)   // u8 → IER
```

类型是 `u8` 的 newtype，加方法约束。每一位对应硬件寄存器的一个标志。编译期保证位运算正确性。

## MMIO 与 NS16550 寄存器

MMIO 把设备寄存器映射到 CPU 物理地址空间。CPU 用普通 load/store 访问设备。

NS16550 寄存器空间 0x00-0x07 共 8 字节：

| offset | 寄存器 | 缩写 | 用途 |
|---|---|---|---|
| 0 | RBR/THR | 接收/发送缓冲 | 读写数据 |
| 1 | IER | 中断使能 | 控制 4 个中断源 |
| 2 | ISR/FCR | 中断状态/FIFO 控制 | 读 ISR/写 FCR |
| 3 | LCR | 线路控制 | 数据位/停止位/校验 |
| 4 | MCR | 调制解调器控制 | RTS/DTR 等 |
| 5 | LSR | 线路状态 | 数据就绪/错误/THR 空 |
| 6 | MSR | 调制解调器状态 | CTS/DSR 等 |
| 7 | SCR | 暂存 | 自由使用 |

`stride` 是相邻寄存器的地址间隔。NS16550 是字节寻址设备，stride=1。

## 三个问题

**Q1. `SpinNoIrq` 锁住了什么？copier 拿到锁时 ISR 会发生什么？**

锁住 UART 寄存器的访问权。copier 持锁期间关闭 sstatus.SIE，ISR 不进入。ISR 触发时已自动关中断，再调 `update_ier` 不死锁。

**Q2. `lazy_static!` 在中断上下文初始化会怎样？**

不会死锁（`Once` 自旋 + 构造无副作用），但不该这样设计。`init_uart_hardware()` 在 ISR 注册前触发。

**Q3. 为什么 `UART_PORT` 单独做成 `static`，不复用 `UART`？**

`ArceOsUartPort` 多封装了 `ier_cache: AtomicU8`，实现 IER 单 owner 模式。

## 经验

- 入口设计：3 泛型折叠为单一类型别名，调用方零泛型负担。
- 全局实例三件套：`SpinNoIrq`（中断安全）+ `lazy_static!`（延迟初始化）+ `&'static`（生命周期）。
- stride=1 铁律：NS16550 仅 8 字节寄存器，stride=4 是 LoadFault 根源。
- IER 单 owner：所有 IER 修改都走 `update_ier`，集中避免不一致。
- 语言特性要点：`lazy_static!` 用 `Once + Deref`；`SpinNoIrq` 用 sstatus 寄存器；`Bitflags` 是 `u8` 的 newtype。
- 硬件知识要点：MMIO 把设备映射到物理地址；NS16550 共 8 寄存器；IER 4 位中断使能。
