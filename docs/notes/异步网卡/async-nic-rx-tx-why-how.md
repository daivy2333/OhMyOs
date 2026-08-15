# 网卡收发的异步化：为什么做、怎么做

**日期**：2026-08-15
**标签**：rust, async, network, nic, interrupt, queue, backpressure

## 为什么做这件事

网卡负责收发网络数据，一个包最多 1500 多字节。老做法是定时查看（轮询）：系统每隔一小段时间去问网卡"有没有新数据"。没有数据时，这些查看是空转，处理器白忙。

异步化的目标：设备有数据时才通知系统（中断），系统唤醒一个后台任务去取；没有数据时后台任务睡觉，不占用处理器。

这条路径和之前做的异步串口类似，但网卡搬运的是整个包而不是字节，而且设备侧有自己的内存队列，规则更多。

## 怎么做

**1. 分层：中断只唤醒，不搬数据**

中断处理只做最少的事：读设备状态、计数、唤醒后台任务。不碰数据、不碰队列。数据搬运交给后台任务，两边职责分开，出错时容易定位。

**2. 唯一后台任务**

数据搬运只由一个后台任务负责，每次最多处理固定数量的包（预算），处理完就睡，等下一次唤醒。

**3. 固定容量的内存槽位**

收发包各 64 个槽位，每个槽位最大 1514 字节，启动时一次性分配。内存占用有上界，不会随流量增长。

**4. 满了就让对方等（背压）**

槽位满了返回"忙"，上层停下来等；有槽位空出来再继续。不会无限排队，也不会悄悄丢包。

**5. 完成账本**

每个发出去的包有编号，完成一个回收一个，一一对应。对不上就报错并保持状态，不重复回收、不谎报完成。

**6. 冲刷**

提交一批数据后，可以等待这一批全部真正发完，而不是"塞进队列就算成功"。

**7. 通知抑制**

一批包只通知一次，避免高频小包下的中断风暴。

**8. 先写测试再写代码**

先用测试定义期望、观察失败，再实现到通过。并发竞态类测试重复跑 100 次，防止偶发问题漏网。

**9. 验证程序与状态快照**

验证程序用固定判定标准，能证明真的发了指定数量的数据、在截止时间内完成；状态快照让内部账本可见，还能暂停/放行某一阶段，用来复现"槽满"。

## 代码在哪

- 中断入口：[kernel/src/drivers/virtio_net_irq.rs](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/drivers/virtio_net_irq.rs)
- 中断逻辑（可单独测试）：[kernel/src/drivers/virtio_net_irq_logic.rs](https://github.com/daivy2333/StarryOS/blob/net-k3/kernel/src/drivers/virtio_net_irq_logic.rs)
- 异步收包：[crates/axnet/src/async_rx.rs](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/async_rx.rs)
- 后台服务与唤醒：[crates/axnet/src/service.rs](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/service.rs)
- 固定槽位队列：[crates/axnet/src/device/fixed_queue.rs](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/device/fixed_queue.rs)
- 冲刷：[crates/axnet/src/flush.rs](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/flush.rs)
- 传输无关的队列契约：[crates/axdriver_net/src/lib.rs](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axdriver_net/src/lib.rs)
- 虚拟网卡驱动适配：[crates/axdriver_virtio/src/net.rs](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axdriver_virtio/src/net.rs)
- 验证程序：[tests/ms05_data_plane_probe.c](https://github.com/daivy2333/StarryOS/blob/net-k3/tests/ms05_data_plane_probe.c)

## 现在的进度

- 收包异步化：完成，模拟器验证通过。
- 双向数据面：主体代码和自动验证完成 22/25 项；还剩验证记录标准的重新设计、在模拟器里手工跑真实收发场景，以及最终核对。

## 参考

- [VirtIO 网卡队列机制入门](virtio-net-queue-intro.md)：环形队列、描述符与通知机制
- [异步网卡架构探索](async-nic-architecture-exploration.md)：整体分层与方案取舍
