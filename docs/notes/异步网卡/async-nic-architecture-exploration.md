# 异步网卡架构探索

**日期**：2026-08-08
**标签**：rust, async, network, nic, virtio, smoltcp, architecture, driver

> 来源：StarryOS `net-k3` 分支的分析文档、项目规范和已有实现。
> 当前处于异步网卡开发前期——同步轮询基线与测试矩阵已完成，异步 RX 队列计划待启动。

## 异步网卡长什么样

异步网卡驱动按四层分离，每层有独立执行上下文和所有权边界：

```text
应用层 socket syscall（poll / select / read / write）
        │
  socket readiness 层  ← 多 waiter、overflow、close、error
        │                 smoltcp 单槽 waker → axpoll::PollSet
        │
  stack runner 层      ← 独立推进 smoltcp ingress/egress/maintenance/timer
        │                 device、software、timer 三类唤醒源
        │
  queue task 层        ← 有界 budget，RX reap/refill、TX submit/reclaim
        │                 packet slot、occupancy、drop reason
        │
  ISR 层               ← 读 cause → ack/mask → AtomicWaker::wake() → 返回
        │                 不分配内存、不阻塞、不 await、不碰 descriptor
        │
  ┌─────────────────────────────────────────┐
  │  平台层：probe、IRQ、DMA、MMIO          │
  │  QEMU VirtIO-MMIO / VF2 DWMAC           │
  └─────────────────────────────────────────┘
```

关键：平台层更换（MMIO → PCI → DWMAC）时，队列所有权、背压和完成语义不变。transport 适配只发生在 probe、IRQ 和 DMA 边界。

对比 UART：UART 是字节 ring + 单一 copier 任务。网卡是 descriptor ring + per-queue service task，以 DMA descriptor 为所有权单位。

## 用到的库

| 库 | 版本 | 角色 |
|---|---|---|
| `axtask::future` | 0.3.0-preview.2 | 异步调度器，`block_on` + `poll_io` |
| `embassy-sync::AtomicWaker` | 0.6.2 | ISR 中安全唤醒任务，O(1) 原子操作 |
| smoltcp | 0.13.1（本地 fork） | TCP/IP 协议栈，本地化在 `crates/smoltcp/` |
| axnet | 本地化 | 网络设备抽象，本地化在 `crates/axnet/` |
| axpoll | 0.1.2 | socket poll/select 事件通知 |

不引入 embassy-executor（与 axtask 调度器冲突）。不引入完整 embassy-net（替换代价过大且首版 MVP 不需要）。

`AtomicWaker` 是唯一已批准的 embassy 依赖。ISR 中 `WAKER.wake()`，任务上下文中 `WAKER.register(cx.waker())`。单 waiter 场景够用；多 waiter 场景由 socket readiness 层的 axpoll 管理。

## 各层职责

### ISR 层

只做四件事：读 cause 寄存器 → 分类 → ack/mask → AtomicWaker::wake()。不读取 descriptor、不搬运数据、不调用协议栈。

中断诊断阶段已验证 VirtIO-MMIO 中断可重复投递且独立于 UART IRQ。IRQ 7 诊断 handler 成功分类 used-buffer / config-change 并正确 ack/rearm。

### queue task 层

唯一 task 推进一个 RX queue 或 TX queue。以固定 budget 回收 TX、处理 RX、补 descriptor。budget 耗尽且有剩余工作时主动重排自身，不立即 unmask IRQ。

RX 状态机：Empty → PostedToDevice → DeviceOwned → Completed → CpuSynchronized → StackToken → Recycled

TX 状态机：Free → StackToken → ReadyToSubmit → DeviceOwned → Completed → Reclaimed → Free

packet slot 有明确容量上界。满载时产生背压（WouldBlock），不通过无界队列隐藏压力。

### stack runner 层

在任务上下文调用 smoltcp poll。响应三类唤醒源：

- device wake：queue task 完成 RX/TX 后
- software wake：socket 有新数据或空间
- timer wake：smoltcp 协议定时器（TCP 重传、ARP 刷新）

空闲时不轮询（与当前 10ms polling fallback 不同）。持续流量不饥饿任一方向。

### socket readiness 层

smoltcp 单个 socket set 只有一个 waker 槽。需要多个 socket 各自等待时，由该层做桥接：

```text
smoltcp socket events → axpoll::PollSet → 多 waiter poll/select
```

覆盖多 waiter、overflow、close、error 场景。poll/select 返回的 readiness 与实际 I/O 一致。

## UART 经验迁移

UART 异步驱动已验证的经验可迁移到 NIC：

| 可迁移 | 迁移方式 |
|---|---|
| ISR 极简原则 | 读 cause → ack → wake，不变 |
| register-recheck | IRQ/descriptor rearm 后重检 |
| backpressure | ring full → WouldBlock → 注册 waker |
| completion 分层 | 区分 submit、doorbell、DMA 完成、descriptor 回收 |
| QEMU/真板分证据 | QEMU 单 hart 不能替代真板 SMP/DMA 证据 |

不可迁移：字节 ring 布局、单一 copier 任务模型。NIC 以 DMA descriptor 和 packet buffer ownership 为基本单位。

NIC 新增的 UART 不具备的问题：DMA cache coherence、scatter-gather、MTU、checksum offload、link state、burst traffic、multiqueue、interrupt moderation。

## 执行上下文契约

每层有明确的所有权边界：

| 对象 | 唯一修改者 | 可观察者 | 回收点 |
|---|---|---|---|
| IRQ mask/cause | ISR / control path | queue task | queue drain/rearm |
| RX descriptor | queue task / device | stack token | token consume/recycle |
| TX descriptor | queue task / device | stack runner | completion + DMA sync |
| stack poll state | stack runner | socket / VFS | socket operation |
| reset generation | control path | IRQ、queue、token | old completion discarded |

## 已完成的基线

| 阶段 | 内容 | 状态 |
|---|---|---|
| 协议栈本地化 | smoltcp/axnet 本地化，14/14 QEMU PASS | ✅ 完成 |
| 轮询网络 | VirtIO-MMIO 轮询端到端，10ms polling fallback | ✅ 完成 |
| 中断诊断 | MMIO IRQ 诊断，12/12 gates PASS | ✅ 完成 |
| 测试矩阵 | 测试矩阵与资格操作手册收口 | ✅ 完成 |

## 下一条路径：异步 RX 队列

下一个阶段覆盖 IRQ 唤醒原语 + QEMU 异步 RX。目标：MMIO RX 由最小 ISR 唤醒唯一 queue task，以有界 budget 推进。

具体引入 `NetQueueControl`、AtomicWaker、register-recheck 竞争防护。验证 event-before-register、register-during-event、spurious IRQ 无 lost wakeup。

验证边界：单向 RX burst 无 busy loop、饿死或 descriptor 泄漏。budget exhausted 可观测。

不含：异步 TX、最终 packet slot、stack runner、socket readiness。

## 参考

- [异步网卡路线分析](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/analysis/starryos-async-network-roadmap.md)
- [四层架构规范](https://github.com/daivy2333/StarryOS/blob/net-k3/openspec/specs/project-model/spec.md)
- [UART 经验迁移规则](https://github.com/daivy2333/StarryOS/blob/net-k3/openspec/specs/knowledge/spec.md)
- [IRQ + waker 架构分析](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/analysis/starryos-device-specific-irq-waker-architecture.md)
- [Embassy 网络模块评估](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/analysis/embassy-network-module-evaluation.md)

