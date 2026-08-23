# 异步开发路径：骨架层可复用，肉层是设备特定的

**日期**：2026-08-23
**标签**：rust, async, driver, uart, nic, skeleton, framework

> 来源：StarryOS `crates/uart_16550/src/async_/isr.rs`、`crates/axnet/src/async_rx.rs`
> 范围：UART 与 NIC 异步路径在 6 个分段上的异同；骨架层与设备特定层的边界；两段解耦的触发条件与边界。

## 结论

ISR → 识别原因 → 唤醒 waker → 调度 async task 这 4 段骨架层跨 UART 与 NIC 模式相同（共享 `embassy_sync::AtomicWaker`）。读/写数据结构（段 5）和协议栈/应用层（段 6）设备特定，代码不能复用。

骨架层目前每设备实现一份，抽成 framework 的想法已经记入项目 backlog，触发条件是 QEMU 多 hart 验证完成 + 至少一个非 NIC 的 async 设备稳定。OS 适配层（`OsRuntime` / `OsWakerSet` / `OsTime` / `OsSleep`）的抽离是另一条 backlog 思路，触发条件仅 QEMU 多 hart 验证完成。

## 路径分 6 段

把"ISR 收到中断到应用拿到数据"拆成 6 段：

| 段 | 内容 | 跨设备 |
|---|---|---|
| 1 | ISR 收中断信号 | 模式相同 |
| 2 | 读寄存器识别中断类型 | 模式相同 |
| 3 | 唤醒 waker | 同一个 `embassy_sync::AtomicWaker` |
| 4 | 调度 async handler | 模式相同 |
| 5 | 读/写数据结构 | 完全不同 |
| 6 | 到协议栈/应用 | 完全不同 |

段 1-4 是骨架，段 5-6 是肉。

## UART vs NIC 在 6 段上的实际差异

| 段 | UART | NIC |
|---|---|---|
| 1 | 16550 IIR 寄存器触发 | VirtIO ISR / DWMAC DMA status 触发 |
| 2 | 读 IIR 判断 RX / TX / line status | 读 ISR 寄存器判断 RX / TX / link / fault |
| 3 | `AtomicWaker::wake()` 唤醒 RX/TX/DRAIN 三个 waker | `AtomicWaker::wake()` 唤醒 queue waker 和 stack waker |
| 4 | RX copier / TX copier 任务被调度 | queue task / stack runner 被调度 |
| 5 | `RingBuf::push/pop`（SPSC、1024B、字节流、无显式 ownership） | `RxSlot::reap/refill` + `TxSlot::submit/reclaim`（64 frames、packet、cookie 显式 ownership） |
| 6 | tty line discipline → `embedded_io::{Read, Write}` | smoltcp `Interface` + `SocketSet` + `SocketHandle` → `TcpSocket` / `UdpSocket` |

段 1-4 写法虽然每设备重写，但遵循同一套契约；段 5-6 没有共性。

## 骨架层已经内化的 4 个契约

UART 的 RX/TX copier 循环和 NIC 的 queue task / stack runner 都遵守：

1. **ISR 不搬数据**——只做 cause / ack / mask / wake，数据搬运留给硬件或拷贝任务。
2. **register-recheck**——`waker.register()` 之后立即重查硬件状态，事件已发生就当作已唤醒。
3. **bounded budget**——每轮工作有上限（UART 的 NAPI 阈值 16 + batch 64；NIC 的 `STACK_STAGE_BUDGET=32`），剩余 backlog 显式可见。
4. **drop guard before wake / Pending**——所有 `Service` / `SocketSet` / listener 锁在 self-wake 或返回 `Pending` 之前先 drop。

NIC 多一个 `generation` 机制（UART 不需要）。原因是 NIC 的 queue 事件有"代际"语义——多次 publish 之间必须保证 register 不被覆盖。UART 的 waker 注册时机在 copier 启动时一次性完成，规避了同类问题。

## 段 5 不能抽的 4 个理由

数据结构层的差异在 4 个维度上：

- **work item 类型**：字节（UART）vs packet（NIC）vs block（SDMMC）vs fence（GPU）——没有共用类型
- **completion 语义**：FIFO drain（UART）vs cookie 回收 + generation 匹配（NIC）vs PRP/SGL 完成（SDMMC）vs fence signal（GPU）
- **backpressure 表达**：`is_full()`（UART）vs `slot_remaining()`（NIC）vs `queue_depth()`（SDMMC）vs `fence_pending()`（GPU）
- **fault 分类**：line status bit（UART）vs queue full / DMA 错误 / link flap / generation 不匹配（NIC）vs DMA terminal events（SDMMC）

这 4 个维度的答案在不同设备之间没有重叠。强行抽 trait 必然退化成"啥都做不了"，或者 associated types 爆炸。

## 段 6 没有共性

协议栈/应用层没有共性可言。UART 的上一层是 tty / raw byte 通道；NIC 的上一层是 TCP/UDP 状态机；block 的上一层是 VFS。这一层没有抽象价值。

## 两段解耦的触发条件

异步开发路径的解耦分两段，都没开工：

| 范围 | 触发条件 | 写明的边界 |
|---|---|---|
| OS 适配层（`OsRuntime` / `OsWakerSet` / `OsTime` / `OsSleep`） | QEMU 多 hart 验证完成 | smoltcp 保留在库内；只抽 6 个 ArceOS 依赖 |
| 骨架层抽象可行性 | QEMU 多 hart 验证完成 + 至少一个非 NIC 的 async 设备稳定 | 包含"数据结构层无法对齐则本思路失败"的退出条件 |

骨架层抽象的触发条件更严：必须等第二个 async 设备出现（block / GPU / USB 之一）才能评估。理由是当前只有 UART 和 NIC 两个用户，模式可能还是"两个巧合"，不是"通则"。

## 真要抽时能做什么

骨架层抽象有 3 个候选层次，都不在当前 backlog 的实现范围内：

1. **ISR handler trait**——`fn service(&mut self) -> IrqEvent`，锁住"ISR 不搬数据"契约，UART 与 NIC 各实现一份。
2. **async task 代码模式**——把"bounded budget + register-recheck + drop guard + lifecycle"做成代码模板或 macro，但不抽成 trait（trait 会丢失 `&mut` 灵活性）。
3. **OS adapter**——`OsRuntime` / `OsWakerSet` / `OsTime` / `OsSleep`，已在 OS 适配层抽离的范围。

数据结构层（段 5）和协议栈层（段 6）是设备特定的，不进入抽象范围。

## 时间窗口

按当前 change 顺序，OS 适配层抽离最早 QEMU 多 hart 验证完成之后启动；骨架层抽象评估最早 QEMU 多 hart 验证完成 + 第二个 async 设备完成。QEMU 全部工作完成前不要动手。

抽得太早的风险：把 QEMU 阶段还在稳定的契约（budget 大小、generation 语义、register-recheck 时序）写进 framework，事后改一个常量都要 framework 升级。

## 关联

- [两种异步驱动范式对比：UART copier 流式 vs SDMMC 请求-响应](async-driver-paradigm-comparison.md)：同一目录下，UART vs SDMMC 的另一组对比
- [OS 抽象具体实现](../异步串口/async-os-abstraction.md)：`OsRuntime` + `OsWakerSet` 2 trait 模式
- StarryOS 项目对异步 NIC 的分层约束（ISR / queue task / stack runner / socket readiness 四层分离）
