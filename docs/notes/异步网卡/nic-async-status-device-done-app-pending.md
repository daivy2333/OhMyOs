# 网卡异步化：全链路异步已打通

**日期**：2026-08-22（2026-08-29 更新至应用侧完成）
**标签**：rust, async, network, nic, driver, socket, polling, syscall

> 完成 MS4（收包异步化）、MS5（有界双向数据面）与 MS6（应用可见的异步网络）之后，网卡收发从设备侧到应用侧全链路异步化已经打通。本文说明这条链路的每一层怎么异步化、怎么做、最终卡点在哪。

## 当前状态：全链路异步

```
设备侧（异步）                  协议栈（异步）                  应用侧（异步）
────────────────────            ──────────────────────        ─────────────────────
硬件中断 ─wake─> 队列后台任务       常驻栈 runner（独立推进）        就绪状态桥
   │                    │                     │               （bridge 到 select/poll）
   │              固定槽收发                   │                   │
   └── 中断只唤醒、不搬数据        smoltcp 三层推进            多等待者扇出
                                  （入/出/维护 + 定时器）        读写就绪与终态
```

收发包不再靠调用方一层层"带着走"：

- **硬件到设备队列**：数据到达有中断，队列后台任务被唤醒取走，空闲时任务休眠。ISR 只做状态读取、计数、唤醒，不搬数据，每轮处理固定预算后休眠——异步。
- **协议栈（smoltcp）**：由常驻后台任务独立推进接收进入、发送输出、定时维护（ARP 过期、TCP 重传等），设备中断、软件事件、定时器都能唤醒它，空闲时睡眠不空转——异步。
- **应用读数据**：socket 的就绪状态经桥接到系统的 select/poll，多程序各等各的 socket 都能被正确唤醒，轮询后拿到的就绪结果和实际收发一致——异步。

## 设备侧异步链路（已完成，MS4/MS5）

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

收发包各有固定数量的槽位，启动时一次性分配，每个槽可容纳一包数据，内存占用有硬性上限。队列满时让上层等待，空间释放后自动恢复。每个发出的包带编号，完成一个回收一个；对不上立即报告并保持现状。提供冲刷能力：等待一批数据全部真正发送完成，而非入队即视为成功。

- 验证程序：[`tests/ms05_data_plane_probe.c`](https://github.com/daivy2333/StarryOS/blob/net-k3/tests/ms05_data_plane_probe.c)（六种场景：只收、只发、双向、接收满、发送满、冲刷确认）

## 应用侧异步化（已完成，MS6）

改造前，应用侧是"半异步"的卡点：协议栈推进完全依赖 socket 操作主动调用（每次 `poll()`/`read()`/`write()` 都同步调 `poll_interfaces()`），且每个 socket 只有一个单槽 waker，多个程序各自等待无法一起唤醒。MS6 分两步解决，对应用不可见，改的是内核内部。

### 1. 协议栈独立推进

新增一个常驻后台任务 [`crates/axnet/src/stack_runner.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/stack_runner.rs)，独立推进 smoltcp 的接收进入、发送输出与定时维护三类工作。设备事件、软件事件、定时器都能唤醒；空闲时睡眠，不空转。

通知用带 generation 计数和 `AtomicWaker` 的事件对象做成，设备进度与软件改动共用同一个 generation，且与 `async_rx` 里队列属主的 generation 分开——设备进度唤醒栈推进，但不改变队列所有权。另留 10ms 兜底轮询处理边界情况。

### 2. 就绪状态桥到 select/poll

smoltcp 内部只有单槽的一次性收发 waker，一个 socket 只能被一个等待者唤醒。新增 [`crates/axnet/src/readiness.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/readiness.rs) 的 ReadinessBridge：每个 TCP/UDP handle 持一个共享桥，分成读、写、终态三组 PollSet；smoltcp 的收发 waker 指向桥，由桥把所有注册的等待者都扇出唤醒。隐藏的监听 socket 不进公共注册表。

终态用稳定的 1~9 代号编码（对应 `DevError`，9 是连接被拒），以原子值保存跨发布存活，保证轮询之后实际收发拿到的错误与就绪结果对得上。

## 下一步：故障恢复语义（MS7）

全链路异步打通的是正常收发路径，没有回答"这条路断掉时怎么收尾"：设备复位后谁持有缓冲、请求被取消后各层怎么回收、包永远不回来时怎么判定超时、上个时代的完成包会不会落到新会话头上。MS7 在单 hart QEMU 下闭环这些场景，保证断在任意一步都不产生悬垂引用、重复回收、永久挂起或静默丢包。目前已完成理解与局部实现，尚未收口。

围绕的机制：

- **唯一恢复属主**：正常时队列任务负责收发回流；设备进入 Fault 后，由一个常驻恢复属主一直持有资源并驱动 bounded 恢复步（quiesce / reset / reinit 各自带 1s、2s、2s 的绝对 deadline），失败时保持故障态、保留 backing，不退出也不重生任务。属主与回收生命周期见 [`crates/axnet/src/async_rx.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/async_rx.rs)。
- **分层取消**：取消一个正在等结果的 waiter、撤销尚未提交的包、强制 quiesce 已归设备的包，三者分别回收，每个 `{stage, cause, queue_epoch, owner}` 都只归属一处。参考承载 stage/progress/ledger 的 transport-neutral 恢复契约与 epoch 化 `TxCookie`：[`crates/axdriver_net/src/lib.rs`](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axdriver_net/src/lib.rs)。
- **epoch 账本**：每次复位是一个新的 QueueEpoch，`TxCookie` 带 epoch+ticket。迟到的完成消息若不属于当前 epoch 就丢弃，绝不回收新 epoch 的物品——这是防 UAF/防重复回收的边界线。
- **link flap 的开关门**：链路断开关闭当前 SocketEpoch（拒绝新提交、取消 pending），但继续回收已归设备的包；链路恢复只开新会话入口，不复活旧 socket，也不自动触发整设备复位。错误按数据面 epoch 隔离，映射为 `NotConnected`/`ConnectionReset`/`TimedOut`/`Interrupted`。

选单 hart QEMU 先做，是因为恢复与并发竞态要在可以故障注入的模型里先闭环，再放大到多核（MS8）和真板，避免直接上手时无法定位归属。

## 参考

- [网卡收发的异步化：为什么做、怎么做](async-nic-rx-tx-why-how.md)：设备侧改造的完整动机与方法
- [异步网卡架构探索](async-nic-architecture-exploration.md)：整体分层与方案取舍
- [异步串口总体架构](../异步串口/async-uart-overall-architecture.md)：可对照的已完整异步化驱动，两级 waker 与四阶段 drain 思路