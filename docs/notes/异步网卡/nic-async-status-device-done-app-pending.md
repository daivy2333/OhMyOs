# 网卡异步化现状：设备侧完成、应用侧待改造

**日期**：2026-08-22
**标签**：rust, async, network, nic, driver, socket, polling, syscall

> 完成 MS4（收包异步化）与 MS5（有界双向数据面）之后，网卡处于"设备侧异步、应用侧同步"的中间状态。本文说明设备侧异步链路怎么做、应用侧为什么还是同步阻塞、下一步如何异步化。

## 当前状态：一条链路两个半场

```
设备侧（已完成异步）              协议栈（仍是同步推进）          应用侧（仍是同步阻塞）
────────────────────             ──────────────────────        ──────────────────────

硬件中断 ─wake─> 队列后台任务          smoltcp 协议栈              socket read/write
  │                  │                                        （block_on 主动轮询）
  │             固定槽收发                 ▲
  └── 中断只负责唤醒、不搬数据             └────── 由 socket 操作同步驱动
```

收发包到了哪一层就停在那层等：

- 硬件到设备队列：数据到达有中断，队列后台任务被唤醒后取走，空闲时任务休眠——**异步**。
- 协议栈（smoltcp）：socket 的每次 `poll()`/`read()`/`write()` 都同步调用 `poll_interfaces()` 主动推进协议栈，数据没来就一直等——**同步**。
- 应用读数据：`recv` 在 syscall 层 `block_on` 阻塞，等协议栈里有数据才返回——**同步阻塞**。

设备侧忙但协议栈侧等，这是当前"半异步"状态的本质。

## 设备侧异步链路（已完成）

### 中断 → 唤醒 → 队列任务

- 中断入口：[`kernel/src/drivers/virtio_net_irq.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/drivers/virtio_net_irq.rs)、可单测的中断逻辑：[`virtio_net_irq_logic.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/drivers/virtio_net_irq_logic.rs)
- 收包核心（唯一队列任务、reap/refill、budget、register-recheck）：[`crates/axnet/src/async_rx.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/async_rx.rs)
- 后台服务与唤醒合流：[`crates/axnet/src/service.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/service.rs)

中断路径只做三件事：读设备状态、计数、唤醒队列后台任务。不搬数据、不碰队列。搬运由唯一队列任务完成，每轮处理固定预算的数据后休眠。

唤醒用 `AtomicWaker` + register-recheck：无论事件先到还是登记先到，唤醒都不会丢。一批包只唤醒一次，避免高频小包下的中断风暴。

### 有界双向数据面

- 固定槽位队列：[`crates/axnet/src/device/fixed_queue.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/device/fixed_queue.rs)
- 转发与协议分发：[`crates/axnet/src/router.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/router.rs)
- 冲刷（等一批全部发完）：[`crates/axnet/src/flush.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/flush.rs)
- 传输无关队列契约：[`crates/axdriver_net/src/lib.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axdriver_net/src/lib.rs)

收发包各有固定数量的槽位，启动时一次性分配，每个槽可容纳一包数据，内存占用有硬性上限。队列满时让上层等待，空间释放后自动恢复。每个发出的包带编号，完成一个回收一个，一一对应；对不上立即报告并保持现状。提供冲刷能力：等待一批数据全部真正发送完成，而非入队即视为成功。

- 验证程序：[`tests/ms05_data_plane_probe.c`](https://github.com/daivy2333/StarryOS/blob/net-k3/tests/ms05_data_plane_probe.c)（六种场景：只收、只发、双向、接收满、发送满、冲刷确认）

## 应用侧为什么还是同步阻塞

协议栈的推进完全依赖 socket 操作主动调用：

- [`crates/axnet/src/socket.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/socket.rs)：`SocketOps`（同步 send/recv）+ `Pollable`（poll/register）。
- `TcpSocket::poll()` 内部先调 `poll_interfaces()` 同步推进协议栈，再检查就绪状态：每次查询都带着一次全栈推进。
- syscall 层用 `block_on` 阻塞等待：[`kernel/src/syscall/io_mpx/select.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/syscall/io_mpx/select.rs)、[`poll.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/syscall/io_mpx/poll.rs)、[`epoll.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/syscall/io_mpx/epoll.rs)。

表现：应用想读数据，要么轮询查询（每次带全栈推进），要么阻塞在 `block_on` 里等。无论哪种，协议栈都不会在空闲时自行推进——这与设备侧"空闲休眠、有事唤醒"不匹配。

## 下一步：应用侧异步化（MS6）

分两步，对应用侧不可见，改的是内核内部。

### 1. 协议栈独立推进

现在协议栈靠 socket 操作带动（`poll_interfaces` 是同步调用）。改为由独立后台任务推进协议栈的接收进入、发送输出与定时维护三类工作，由设备事件、软件事件和定时器唤醒，空闲时休眠。验收标准：空闲时协议栈不空转，持续流量下不挤占其他任务。

现状入口：[`crates/axnet/src/lib.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/lib.rs) 的 `poll_interfaces()`。

### 2. 就绪状态接入统一等待接口

现在每个 socket 有自己的唤醒登记（`register_waker` 单槽），多程序各自的等待无法正确唤醒。计划把 smoltcp 的唤醒信号桥接到统一的 `axpoll::PollSet`，让 select/poll/epoll 在多个 socket、缓冲区溢出、连接关闭、出错时都能正确唤醒并返回与实际收发一致的就绪结果。

现状：[`crates/axnet/src/tcp.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/tcp.rs) 的 `impl Pollable for TcpSocket`（`poll()` 同步推进 + 单槽 `register_waker`）；目标接口 `axpoll::PollSet`（见异步串口笔记中的 `OsWakerSet` 实现）。

两步都先完成方案与测试用例定义，再开始实现。

## 参考

- [网卡收发的异步化：为什么做、怎么做](async-nic-rx-tx-why-how.md)：设备侧改造的完整动机与方法
- [异步网卡架构探索](async-nic-architecture-exploration.md)：整体分层与方案取舍
- [异步串口总体架构](../异步串口/async-uart-overall-architecture.md)：可对照的已完整异步化驱动，两级 waker 与四阶段 drain 思路