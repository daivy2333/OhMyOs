# ISR 触发后禁用中断

**日期**：2026-06-25
**标签**：rust, os, riscv, interrupt, uart

> 来源：StarryOS `uart_16550/src/async_/isr.rs:71-92` 通读。
> 范围：NS16550 中断机制与 ISR-copier 交接模式。

## 答案

不是防"其他中断嵌套"，是防"同一中断条件反复触发"。

NS16550 是电平触发。RX FIFO 还有数据时，`DATA_READY` 条件一直成立。ISR 不禁用 → 返回后立即再触发 → interrupt storm。

## 触发机制

| 类型 | 触发条件 | 例子 |
|---|---|---|
| 边沿触发 | 信号 0→1 跳变一次 | 少数 PCI 设备 |
| 电平触发 | 信号保持高时持续触发 | NS16550 UART |

## interrupt storm

ISR 不禁中断会形成无限循环：

```
[字节 A 到达 FIFO] → ISR → 返回 → FIFO 仍非空 → 立即再触发 → ...
```

CPU 100% 在 ISR 中，无暇处理任何任务。

## RISC-V trap 机制

RISC-V 中断由 trap 机制处理：

| 寄存器 | 作用 |
|---|---|
| `stvec` | trap 入口地址 |
| `sepc` | trap 时的 PC（用于返回）|
| `scause` | trap 原因（中断/异常/具体类型）|
| `stval` | trap 附加信息 |
| `sstatus` | 状态寄存器（含 SIE 全局中断使能）|

`stvec` 指向 ISR 入口。`sstatus.SIE` 在 trap 时硬件自动清零，返回时自动恢复。

## PLIC 中断控制器

PLIC（RISC-V 标准）把外设中断路由到指定 hart：

```
硬件产生中断
  ↓
PLIC 检测 pending 位
  ↓
PLIC 按优先级派发到目标 hart
  ↓
hart 跳转到 stvec 指向的 ISR
  ↓
ISR 通过 claim 寄存器读取 IRQ 编号
  ↓
ISR 处理
  ↓
ISR 写 complete 寄存器通知完成
  ↓
hart 返回原任务
```

关键寄存器：

- `priority`：每中断优先级。
- `pending`：等待中的中断。
- `enable`：每 hart 的中断使能位。
- `claim`/`complete`：读取 / 通知完成。

## 全局中断 vs 中断源使能

| 控制位 | 位置 | 作用 |
|---|---|---|
| `sstatus.SIE` | CPU 寄存器 | 全局中断开关 |
| `IER.DATA_READY` | UART 寄存器 | RX 中断源开关 |

触发条件：全局使能 AND 该源使能 AND 条件为真。

ISR 返回后 SIE 恢复。但若 IER.DATA_READY=1 且 FIFO 仍非空 → 立即再触发。所以必须软件显式关 IER。

## 三个原因

**防电平触发重入**：ISR 触发 → 关 IER 对应位 → 硬件不再触发同源中断。这是禁用中断的根本原因。

**ISR-copier 交接**：ISR 只做"读 ISR / 禁中断 / wake"，copier 在任务上下文读 FIFO、重开中断。把"快路径"和"慢路径"分开。

**避免寄存器竞争**：ISR 与 copier 同时访问 UART 寄存器会破坏一致性。

## IsrRegisters 与 read_volatile

`isr.rs:23-62` 的 `IsrRegisters` 绕过 `Uart16550` API 读寄存器：

```rust
pub(crate) unsafe fn read_isr(&self) -> ISR {
    let ptr = self.base.as_ptr().add(offsets::ISR);
    ISR::from_bits_retain(ptr.read_volatile())
}
```

不走 `Uart16550::isr()`：该方法需要 `SpinNoIrq` 锁。ISR 中再拿锁违反 ISR 极简原则。

`IsrRegisters` 解决三件事：

- ISR 中读 ISR/LSR 寄存器无需锁。
- 只接受 `NonNull<u8>` 基址，构造时不做任何 I/O。
- `read_volatile` 防止编译器优化掉寄存器读。

`read_volatile` 语义：保证读不被编译器优化为常量。MMIO 必须用此——设备状态可能随时变化。

## 易混点

**禁用全局中断不就够了？** SIE=0 只在 ISR 运行时有效。返回后立刻恢复，中断源条件还在 → 立即再触发。

**其他 UART 中断能来吗？** 单核上不能（全局关闭）。多核另一 CPU 可并发，但另说。

**为什么不全用边沿触发？** 硬件特性决定。NS16550 无此选项。

**QEMU 和真板行为一致吗？** QEMU 模拟电平触发，真板也是电平触发。软件契约相同。

## 经验

- 禁用中断 = 关闭敲门声：告诉硬件"这次处理完了，下次新事件再来"。
- 同源重入是 ISR 设计的隐藏陷阱：默认电平触发的设备必须显式 disable。
- ISR 与任务的分工：ISR 极简（关中断+wake），copier 处理数据搬运。
- RISC-V trap 机制：硬件自动保存现场、sstatus.SIE 自动切换。
- PLIC 路由：外设中断通过 PLIC 派发到 hart。
- `read_volatile` 必要性：设备状态不能被编译器优化。
- `IsrRegisters` 模式：ISR 中绕过高级 API，用 `read_volatile` 读寄存器，避开锁。
