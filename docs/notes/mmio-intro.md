# MMIO：用 load/store 指令操作硬件

**日期**：2026-07-21
**标签**：mmio, hardware, volatile, uart, memory, pma

> 来源：StarryOS `crates/uart_16550/src/backend.rs`、`kernel/src/platform/descriptor.rs`、16550 规范。
> 范围：MMIO 概念、硬件通路、volatile 语义、与 DMA 的配合。

## 背景：天天在用，但到底是什么

StarryOS UART 驱动里，每一行寄存器操作都是 MMIO：

```rust
self.port.write(reg::THR, byte);    // 写 Transmitter Holding Register
let lsr = self.port.read(reg::LSR); // 读 Line Status Register
```

语法上和普通内存访问一样（读一个地址、写一个地址）。但访问的不是 RAM，是 UART 硬件寄存器。

CPU 只有一套 load/store 指令。它怎么知道 `0x1000_0000` 是 RAM 还是 UART 寄存器？答案是地址路由 — 总线译码逻辑而非 CPU 来决定。

## 什么是 MMIO

MMIO（Memory-Mapped I/O，内存映射输入输出）把外设的寄存器映射到 CPU 的物理地址空间。CPU 用普通 load/store 指令访问外设寄存器，就像访问内存一样。

物理地址空间布局（简化）：

```
┌─────────────────────────────────────┐
│ 0x0000_0000                         │
│        DRAM（真正的内存）            │  ← 走内存控制器
│                                     │
│ 0x8000_0000                         │
│        ...未使用...                  │
│ 0x1000_0000  ─── UART0 RBR/THR     │  ← 走总线桥到 UART
│ 0x1000_0004  ─── UART0 IER/DLH     │
│ 0x1000_0008  ─── UART0 ISR/FCR     │
│        ...                          │
│ 0x0C00_0000  ─── PLIC 基地址        │  ← 走总线桥到 PLIC
└─────────────────────────────────────┘
```

CPU 执行 `load 0x1000_0000` 时，不关心此地址后面是 RAM 还是 UART。地址译码逻辑根据地址范围决定路由到哪个设备。

## PMIO 对比：为什么 MMIO 胜出

历史上 CPU 访问外设有两种方案。

**PMIO（Port-Mapped I/O）**。x86 的遗产。I/O 有独立地址空间，需要专用 IN/OUT 指令：

```asm
MOV DX, 0x3F8      ; I/O 端口号
IN  AL, DX         ; 从端口读（不是从内存读）
OUT DX, AL         ; 写到端口
```

优点：不占用内存地址空间。缺点：需要专用指令、寄存器数量有限（x86 65536 个）、编译器优化困难。

**MMIO**。RISC-V、ARM、多数现代架构的选择。I/O 寄存器映射到统一地址空间：

```asm
lui  t0, 0x10000     ; t0 = 0x1000_0000（UART 基地址）
lb   t1, 0(t0)       ; 和读内存完全一样的指令
sb   t2, 0(t0)       ; 和写内存完全一样的指令
```

| | PMIO | MMIO |
|---|---|---|
| 地址空间 | 独立 I/O 空间 | 统一内存地址空间 |
| 指令 | 专用 IN/OUT | 普通 load/store |
| 使用架构 | x86 传统 | RISC-V、ARM、现代 x86 也支持 |
| StarryOS | 不使用 | 全部外设都用 |

## 硬件通路：一次 MMIO 写入的完整路径

CPU 执行 `store 0(t0)`，`t0 = 0x1000_0000` 时：

```
CPU core
  │  store 指令
  ▼
Load/Store Unit (LSU)
  │  查页表：VA → PA 0x1000_0000
  │  查 PMA：这个地址是 DEVICE 类型，不是 RAM
  ▼
总线互连（Bus Interconnect / Crossbar）
  │  地址译码：0x1000_0000 落在 UART0 的地址窗口
  ▼
UART 设备
  │  偏移 0x00 → 对应 THR 寄存器
  │  数据锁存到 THR，设备开始发送
```

三个硬件机制保证正确性：

**地址译码**。总线上的译码逻辑维护地址范围→设备的映射。CPU 发出地址后，只有一个设备认领。

**PMA（Physical Memory Attribute）**。RISC-V 规范为物理地址定义属性：Main Memory、I/O、Reserved。MMIO 地址标记为 I/O。这影响 CPU 行为 — 不对 I/O 地址做推测执行、不合并或重排 I/O 访问、不使用 cache。

**页表映射**。OS 通过页表项的 PBMT 位（RISC-V）将 MMIO 页面标记为 DEVICE 内存类型。D1 bring-up 时配的 C906 PTE 属性就是干这个的。

## volatile：让编译器别自作聪明

编译器不知道 `0x1000_0000` 是硬件寄存器。它会像优化普通内存访问一样优化 MMIO：

```rust
// 如果不用 volatile
fn bad_write(port: &mut UartPort, byte: u8) {
    port.base().write(THR, byte);  // 编译器：写了一次
    port.base().write(THR, byte);  // 编译器：又写同一地址，第一次没意义，删掉
}
// 后果：两个字节只发了一个
```

`volatile` 对编译器施加四个约束：

| 约束 | 含义 | 无 volatile 的风险 |
|---|---|---|
| 不删除 | 每次访问生成真实指令 | 编译器认为"写了没人读"就删掉 |
| 不合并 | 两次写不能合成一次 | 应发送两个字节，合并后只发一个 |
| 不重排 | 多个 volatile 访问顺序不可交换 | 先写 LCR 再写 DLL，重排后波特率配置错乱 |
| 不缓存 | 不能把值放寄存器复用 | 读 LSR，编译器用寄存器缓存旧值，看不到 THRE 变化 |

`volatile` 只约束编译器，不约束 CPU 硬件。多核下的内存可见性由原子操作内存序（Acquire/Release）保证 — 这是两个独立层面。

## StarryOS 中的 MMIO 实例

### UART 16550 寄存器布局

8 个寄存器，按偏移访问（来自 `crates/uart_16550/src/spec.rs`）：

```
偏移    寄存器    读              写
0x00    RBR/THR   接收缓冲        发送保持
0x01    IER       中断使能        中断使能
0x02    ISR/FCR   中断状态        FIFO 控制
0x03    LCR       线路控制        线路控制
0x04    MCR       Modem 控制      Modem 控制
0x05    LSR       线路状态        （只读）
0x06    MSR       Modem 状态      （只读）
0x07    SCR       暂存器          暂存器
```

注意：0x00 的 RBR（读）和 THR（写）共用一个偏移，但物理上是两个独立寄存器。读和写路由到不同硬件。

### 平台描述符中的 MMIO 参数

`kernel/src/platform/descriptor.rs` 的 `ConsoleConfig` 集中了 UART 的 MMIO 参数：

```rust
// 概念结构（简化）
struct ConsoleConfig {
    kind: ConsoleKind,         // NS16550 还是 DW APB UART
    base_paddr: usize,         // MMIO 基地址（物理地址）
    irq: u32,                  // 中断号
    stride: usize,             // 寄存器间距
    width: MmioWidth,          // 访问宽度
}
```

`stride` 是关键参数。NS16550 寄存器是 8 位宽、紧密排列，每个寄存器占 1 字节，`stride = 1`。有些 SoC 的 UART 寄存器按 32 位对齐（每个占 4 字节），`stride = 4`。

### 物理地址到虚拟地址的映射

内核启动后，MMIO 物理地址需要通过页表映射才能访问：

```rust
let uart_paddr: usize = 0x1000_0000;
// axplat 映射：物理地址 → DEVICE 类型的虚拟地址
let uart_vaddr: *mut u8 = axplat::mem::phys_to_virt(uart_paddr);
```

映射时页表项的 DEVICE/nGnRE 属性确保 CPU 不对此区域使用 cache、不做推测执行。

## MMIO 访问的四个硬约束

### 约束 1：访问宽度必须匹配

读 32 位寄存器必须用 32 位 load，不能用 4 次 8 位 load 拼。有些设备按不同访问宽度路由到不同寄存器。你的 UART 驱动中 `width` 参数（`MmioWidth`）保证这一点。

### 约束 2：不能走 cache

如果 MMIO 地址被 cache，读 LSR 可能拿到旧值，看不到 THRE 变化。DEVICE 内存类型告诉 CPU：绕过 cache。PIO UART 不受此影响（`write_volatile` 到 MMIO 天然绕 cache），但 DMA 需要手动 cache clean/invalidate，因为 DMA 操作的是普通内存（可 cache 的 RAM），不是 MMIO。

### 约束 3：访问不能乱序

写 LCR 配置波特率，紧接着写 DLL+DLH 设置分频值。如果 CPU 把两次写重排，波特率配置出错。RISC-V 的 DEVICE 内存类型保证同类型地址的访问不重排。但 DEVICE 和普通内存之间的访问可能重排 — DMA 场景需要 memory barrier。

### 约束 4：读可能有副作用

普通内存读是幂等的，读 100 次返回相同值。MMIO 读可能有副作用：读 RBR（接收缓冲寄存器）同时清空对应中断标志。这意味着不能"顺便读一下" — 每次 MMIO 读都可能改变设备状态。

## MMIO 和 DMA 的配合

这两个概念是互补的，不是替代关系。

| | MMIO | DMA |
|---|---|---|
| 谁发起的 | CPU | DMAC（硬件） |
| 访问什么 | 设备寄存器（控制/状态） | 内存（数据 buffer） |
| 数据方向 | CPU ↔ 设备控制面 | 内存 ↔ 设备数据面 |
| 需要 volatile | 是 | 否（DMAC 自己处理时序） |
| 需要 cache 管理 | 否（DEVICE 绕 cache） | 是（DMA 操作普通内存） |

配合模式：

```
CPU ──MMIO──→ [DMAC 配置寄存器]    ← 控制面：CPU 配参数
                    │
                    ├──总线读──→ [内存 buffer]    ← 数据面
                    │
                    └──总线写──→ [设备 FIFO]      ← 数据面：DMAC 干活
```

CPU 通过 MMIO 写 DMAC 寄存器（源地址、目标地址、长度、启动位），这是控制面。DMAC 自己通过总线搬运数据（读内存、写设备），这是数据面。CPU 在 MMIO 上只做少量配置性访问，数据搬运由 DMA 完成。

## 代码对照

### 一个 MMIO 读-改-写（就是你 ISR 中的 update_ier）

```rust
/// 启用/禁用 RX 中断
fn update_ier(port: &UartPort, rx_enable: bool) {
    // 步骤 1：MMIO 读 — volatile，不缓存
    let ier = unsafe { ptr::read_volatile(ier_ptr) };

    // 步骤 2：CPU 计算新值 — 纯寄存器操作
    let new_ier = if rx_enable {
        ier | IER_RX_BIT   // 置位
    } else {
        ier & !IER_RX_BIT  // 清零
    };

    // 步骤 3：MMIO 写 — volatile，立即生效
    unsafe { ptr::write_volatile(ier_ptr, new_ier); }
}
```

三步中只有步骤 1 和 3 是 MMIO。步骤 2 是纯 CPU 计算。整个 RMW 在 SMP 下需要锁保护，因为另一个 hart 可能同时写 IER。

### MMIO 轮询等待（忙等 THRE）

```rust
/// 忙等直到 THR 为空
fn wait_thre(port: &UartPort) {
    loop {
        let lsr = unsafe { ptr::read_volatile(lsr_ptr) };
        if lsr & LSR_THRE != 0 {
            break;  // THR 空了
        }
        // 真实驱动用中断+waker，不是忙等
    }
}
```

每次 `read_volatile(lsr_ptr)` 真的读硬件，不走 cache。循环越快 CPU 越忙。真实驱动用中断唤醒 + NAPI 批量处理替代忙等。

### MMIO 写多寄存器（配置波特率）

```rust
/// 写波特率分频值：先设 DLAB，再写 DLL+DLH，最后清 DLAB
fn set_baud_divisor(port: &UartPort, divisor: u16) {
    // 步骤 1：设 DLAB = 1
    let lcr = unsafe { ptr::read_volatile(lcr_ptr) };
    unsafe { ptr::write_volatile(lcr_ptr, lcr | LCR_DLAB); }

    // 步骤 2：写低字节和高字节 — 顺序不能乱
    unsafe { ptr::write_volatile(dll_ptr, (divisor & 0xFF) as u8); }
    unsafe { ptr::write_volatile(dlh_ptr, (divisor >> 8) as u8); }

    // 步骤 3：清 DLAB = 0
    unsafe { ptr::write_volatile(lcr_ptr, lcr); }
}
```

所有 MMIO 写必须是 `write_volatile` 且顺序不能重排。volatile 保证编译器不乱序，DEVICE 内存类型保证 CPU 硬件不乱序。

## 常见症状与原因

| 症状 | 原因 | 修复方向 |
|---|---|---|
| 写寄存器无效，读返回全 0 | MMIO 物理地址未映射到页表 | 检查 `mmio_ranges` 注册和页表映射 |
| 读寄存器总是返回旧值 | MMIO 页面被映射为 cacheable | 页表项设 DEVICE/nGnRE |
| 连续两次写，只有第一次生效 | 没用 volatile，编译器删了第二次 | 改用 `write_volatile` |
| SMP 下 IER 位随机丢失 | 非原子 RMW（读写间被打断） | 放 `SpinNoIrq::lock()` 临界区 |
| 读之后中断状态没变 | 某些寄存器读有副作用（如 RBR 读清中断） | 不无故读取有副作用的寄存器 |
| 写入后设备无反应 | stride/width 配错 | 对照数据手册确认寄存器物理布局 |

## 动手问题

1. **stride 的地址计算**：在 `crates/uart_16550/src/backend.rs` 里，`MmioBackend::read()` 怎样结合 `base + offset * stride` 计算最终地址？如果 `stride = 4`，读偏移 0 和读偏移 5 分别访问哪个字节地址？

2. **volatile 的反证**：假设删掉 `write_volatile` 用普通 `write` 代替，连续写两个字节到 THR。release 模式下 `cargo objdump` 对比，编译器可能怎么处理？

3. **MMIO + DMA 时序设计**：设计一个 UART TX 的 DMA 传输流程。标出哪些步骤是 MMIO（CPU 写 DMAC 寄存器）、哪些是 DMA 总线事务（DMAC 读内存、DMAC 写 UART FIFO）、中断何时产生。
