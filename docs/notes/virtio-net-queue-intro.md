# VirtIO 网卡队列机制入门

**日期**：2026-08-01
**标签**：virtio, network, queue, descriptor, event-idx, interrupt, mmio

## 背景：从单个寄存器到环形队列

[MMIO 入门](mmio-intro.md) 讲了 CPU 怎么用 load/store 操作单个硬件寄存器。UART 这类慢设备每次读写一两个字节，一个寄存器就够。网卡一次收发整个包，多则 1500 字节，单寄存器搬不完。

VirtIO 的做法：在内存里放环形队列，driver 和 device 共享。driver 把发包 buffer 指针扔进队列，device 取走发送。device 把收包 buffer 指针扔进另一个队列，driver 取走处理。CPU 不逐字节写硬件，只操作内存中的队列结构。硬件通过队列指针移动感知工作。

## VirtQueue 三件套

一个 VirtQueue 由三块连续内存组成：

```
┌──────────────────────────────────────────────────────────┐
│  Descriptor Table                                        │
│  ┌─────┬─────┬─────┬─────┬─────────────┐                │
│  │ addr│ len │ flg │ nxt │  ...        │  ← 64-256 项   │
│  └─────┴─────┴─────┴─────┴─────────────┘                │
│  每项描述一个物理内存 buffer（地址+长度+标志）            │
├──────────────────────────────────────────────────────────┤
│  Available Ring                                          │
│  ┌─────┬─────┬─────────────────┐                         │
│  │ flg│ idx │  desc_idx[]...  │  ← driver 写            │
│  └─────┴─────┴─────────────────┘                         │
│  "这些 descriptor 我准备好了，device 你来取"              │
├──────────────────────────────────────────────────────────┤
│  Used Ring                                               │
│  ┌─────┬─────┬─────────────────┐                         │
│  │ flg│ idx │  used_elem[]... │  ← device 写            │
│  └─────┴─────┴─────────────────┘                         │
│  "这些 descriptor 我用完了，driver 来回收"                │
└──────────────────────────────────────────────────────────┘
```

三个环各自的职责：

| 结构 | 谁写 | 谁读 | 内容 |
|---|---|---|---|
| Descriptor Table | driver | driver/device | buffer 的物理地址、长度、链 next |
| Available Ring | driver | device | "我准备好哪些 descriptor 给你用" |
| Used Ring | device | driver | "我用完了哪些 descriptor 还你" |

`idx` 是单调递增游标，每次 push 后 +1。环大小固定（通常 256），`idx % size` 是实际槽位。"环形"即用模运算复用固定大小数组。

## 数据流：一次 TX 怎么走

以发送一个 64 字节 ARP 包为例。

**1. driver 准备 descriptor**

driver 在 Descriptor Table 找一个空闲项（比如 index 5），把发包 buffer 的物理地址和长度填进去：

```
descriptor[5].addr = 0x80001000  (包 buffer 物理地址)
descriptor[5].len  = 64
descriptor[5].flags = 0          (单段，无 next)
descriptor[5].next  = 0
```

**2. driver 推进 Available Ring**

driver 把 `5` 写进 Available Ring 的下一个槽位，然后 `avail.idx += 1`：

```
avail.ring[avail.idx % 256] = 5
avail.idx += 1
```

这一步等于告诉 device："descriptor 5 你可以拿了"。

**3. 通知 device（kick）**

driver 写 MMIO 寄存器 `QueueNotify` 触发 device 去看 Available Ring。VirtIO-MMIO 的 kick 是一次 volatile store：

```rust
// 简化：实际在 virtio-drivers crate 里
base.add(0x50).write_volatile(0);  // QueueNotify offset
```

如果不写这次 kick，device 不知道有新工作。kick 是开销点--每次都要 MMIO 写，不能省。

**4. device 处理并推进 Used Ring**

device 看到 `avail.idx` 变了，读 Available Ring 拿到 descriptor index 5，从 descriptor 拿到 buffer 物理地址，DMA 把包发出去。完成后把 `5` 写进 Used Ring，`used.idx += 1`：

```
used.ring[used.idx % 256].id  = 5
used.ring[used.idx % 256].len = 64   (device 实际写的字节数)
used.idx += 1
```

**5. driver 回收**

driver 下次 poll 时比较 `last_used_idx` 和 `used.idx`，发现差 1，读 Used Ring 拿到 index 5，回收 buffer，`last_used_idx += 1`。

```rust
// crates/axnet/src/device/ethernet.rs:266
// https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/device/ethernet.rs#L266
let rx_buf = match self.inner.receive() {
    Ok(buf) => buf,  // ← 从 used ring pop 出来
    Err(err) => { ... }
};
self.inner.recycle_rx_buffer(rx_buf).unwrap();  // ← 把 descriptor 还回 free list
```

## EVENT_IDX：中断抑制协议

如果每次 device 完成 descriptor 都中断通知 driver，高频小包场景 IRQ 风暴。VirtIO 提供两种中断抑制模式：

- **无 EVENT_IDX**：device 每推一个 used 就触发一次中断。
- **有 EVENT_IDX**：driver 在 `avail.used_event` 写 `idx`，表示 device 的 `used.idx` 超这个值才中断。driver 回收后更新 `used_event`，等于 rearm。

StarryOS 当前协商包含 `RING_EVENT_IDX`。这意味着：

- device 完成一批 descriptor 后，比较 `used.idx` 和 `avail.used_event`，超过才中断。
- driver 在 `pop_used` 后把 `last_used_idx` 写入 `avail.used_event`，告诉 device "下次超过这里再通知我"。
- `VirtQueue::set_dev_notify` 在 `event_idx=true` 时不改 flags（不需要 enable/disable notify bit，用 `used_event` 控制）。

完整重复投递链：

```
IRQ handler ACK device cause
  -> PLIC EOI
  -> polling owner 消费 used ring
  -> pop_used 更新 avail.used_event
  -> 下一次 device 事件 -> 下一次 IRQ
```

rearm 不是 IRQ handler 干的，是唯一轮询 owner 在 `pop_used` 后干。IRQ 诊断 handler 不碰 used ring，所以不参与 rearm。

## 中断状态与 ACK 寄存器

VirtIO-MMIO 设备的中断寄存器在 MMIO offset `0x60`（interrupt status）和 `0x64`（interrupt acknowledge）：

| offset | 寄存器 | 读/写 | 含义 |
|---|---|---|---|
| 0x60 | InterruptStatus | R | bit0 = used-ring 事件，bit1 = config-change |
| 0x64 | InterruptACK | W | 写 1 清除对应 status 位 |

IRQ handler 顺序：

```
handler entry
  -> volatile read InterruptStatus (0x60)
  -> 分类 used-ring / config-change / unknown / spurious
  -> write raw non-zero status to InterruptACK (0x64)
  -> read back status for residual witness
  -> update Relaxed telemetry
  -> return
```

ACK 写完不代表 device 不再触发--只要 `used.idx > avail.used_event`，device 在下次 kick 后还会再触发。ACK 只是清当前 pending 位。

## 为什么 irq_num()=None 时反而要保留 polling fallback

这是无 IRQ 轮询阶段的决策。看 `EthernetDevice` 的 trait 实现：

```rust
// crates/axnet/src/device/ethernet.rs:336
// https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/device/ethernet.rs#L336
fn requires_polling(&self) -> bool {
    self.inner.irq_num().is_none()
}

fn register_waker(&self, waker: &Waker) {
    if let Some(irq) = self.inner.irq_num() {
        register_irq_waker(irq, waker);
    }
}
```

`irq_num()` 返回 `None` 时，`register_waker` 不注册 IRQ waker。但 smoltcp 的 `poll_at` 协议定时器（TCP 重传等）也走这条 waker 路径。waker 没注册，定时器到期无人唤醒 `Service::poll`，协议栈卡死。

无 IRQ 轮询的做法：`requires_polling() == true` 时，`Service::register_waker` 在 `iface.poll_at` 之外挂 10ms `sleep_until` 兜底：

```rust
// crates/axnet/src/service.rs:110
// https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/service.rs#L110
let polling_deadline = any_masked_device_requires_polling(
    mask,
    self.router.devices.iter().map(|d| d.requires_polling()),
)
.then_some(timestamp + POLLING_FALLBACK);  // POLLING_FALLBACK = 10ms

let next = select_wake_deadline(protocol_deadline, polling_deadline);
```

两条 deadline 取 `min`：协议定时器更早就等协议，否则最多等 10ms。这样既保住了协议栈推进，又不污染未来的 IRQ waker 路径。

QEMU 单核下空闲 CPU 100-111% 是 10ms 兜底的副作用：每 10ms 唤醒做 `iface.poll`。单核没有别的 hart 分担，timer 中断 + 调度 + poll 跑满 hart。不是 busy loop——busy loop 不让出 CPU 给 `sleep_until`。

## 与 UART 驱动的对比

[异步 UART 驱动](async-uart-driver-architecture.md) 用 SPSC ring buffer + copier task 把用户态字节流搬到硬件 FIFO。VirtIO 网卡结构上像--也是环形队列、也是 driver/device 两侧、也有中断通知。差异在搬运单位：

| 维度 | UART ring buffer | VirtQueue |
|---|---|---|
| 搬运单位 | 字节 | descriptor（指向包 buffer） |
| 队列位置 | 内核空间 SPSC | 物理内存共享 |
| 通知机制 | AtomicWaker + IRQ | kick MMIO + IRQ |
| 批量 | copier pop_batch 1024B | 一次 avail push 多个 descriptor |
| 中断抑制 | NAPI 阈值 16 次 | EVENT_IDX used_event |

UART 的 copier task 在 VirtIO 没有对应物。VirtQueue 的搬运由 driver 操作 avail/used，device 自己 DMA。所以 UART ISR 不碰数据（copier 干），VirtIO IRQ handler 也不碰数据（polling owner 干）。两者都遵"ISR 只唤醒，搬运在任务上下文"。

## 参考

- [MMIO：用 load/store 指令操作硬件](mmio-intro.md)
- [异步 UART 驱动总体架构](async-uart-driver-architecture.md)
- StarryOS `crates/axnet/src/device/ethernet.rs`：`EthernetDevice::requires_polling`、`register_waker`
- StarryOS `crates/axnet/src/service.rs`：`Service::register_waker`、`any_masked_device_requires_polling`
- OASIS Virtual I/O Device (VirtIO) Specification v1.1：VirtQueue 与 EVENT_IDX 规范
