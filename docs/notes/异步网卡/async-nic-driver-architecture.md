# 异步网卡驱动总体架构

**标签**：rust, async, network, nic, virtio, smoltcp, driver, recovery, readiness

> 来源：StarryOS `net-k3` 分支，`kernel/src/drivers/virtio_net_irq.rs`、`kernel/src/syscall/fs/ctl.rs`、`crates/axnet/src/{async_rx.rs,stack_runner.rs,readiness.rs,recovery.rs,flush.rs,service.rs,device/fixed_queue.rs}`、`crates/axdriver_virtio/src/net.rs`、`crates/axdriver_net/src/lib.rs`、`crates/virtio-drivers/`。
> 异步网卡已全链路贯通并经故障恢复闭环，本文以实际实现为准，说明从硬件中断到应用 socket 每一层怎么异步、怎么在故障下安全收尾。不涉及 OS 适配层与真板细节。

## 数据流全景

网卡以**描述符（descriptor）**和**固定槽位**为所有权单位，从硬件到应用一共五层：

```
用户进程（poll / select / read / write / flush）
        ▲                                   │
        │        就绪桥 readiness           │ socket 操作
        ▼                                   │
      socket（TCP / UDP，wrapper 内持 ReadinessBridge）
        ▲                                   │
        │       就绪/终态事件                │ 提交数据（write）
        ▼                                   │
   栈 runner（独立推进 smoltcp 收发与维护）──►  Router 分发
        ▲                                   │
        │       包就绪                        │ 出栈帧
        ▼                                   ▼
   队列 owner（唯一后台任务：reap→收 / refill→补 / submit→发 / reclaim→还）
        ▲                                   │
        │      used 完成 / 新描述符         │ 描述符 + 数据
        ▼                                   ▼
   设备层 VirtIO（virtq 收发队列、DMA、link/复位状态）
        ▲                                   │
        │     IRQ：used / config-change      │ 设备写回 used-ring
        ▼                                   ▼
     硬件 ISR（读 cause → ack/mask → AtomicWaker.wake()）
```

方向相反、上下对称。与串口最大的差别：这里中间隔了一个**协议栈**，所以收发两侧各多出"栈 runner 推进"和"就绪桥"两层；同时因为是整包 + 描述符 + DMA，恢复与所有权归比字节流复杂得多。

## 分层总览

| 层 | 执行上下文 | 职责 | 主要实现 |
|---|---|---|---|
| ISR | 中断上下文 | 读 cause、ack/mask、唤醒，不搬数据 | `virtio_net_irq.rs` + `virtio_net_irq_logic.rs` |
| 队列 owner | 唯一后台任务 | 收（reap/refill）与发（submit/reclaim）的预算制推进、常驻恢复 | `async_rx.rs`、`device/`、`recovery.rs` |
| 栈 runner | 常驻任务 | 推进 smoltcp 的 ingress/egress/maintenance/timer | `stack_runner.rs` |
| 就绪桥 | 任务上下文 | 把单槽 waker 展开成多等待者，就绪/终态状态 | `readiness.rs`、`wrapper.rs` |
| 设备层 | 驱动 | virtq、DMA、有界复位、link 快照 | `axdriver_virtio`、`axdriver_net`、`virtio-drivers` |

## 设备层：VirtIO、DMA 与有界复位

网卡走 VirtIO-MMIO，一个收发描述符环形队列配 DMA 内存。三个 crate 分工：

- `crates/virtio-drivers/`：MMIO transport、virtqueue、DMA。`Dma::new` 负责分配并**清零整段 DMA region**后再交给设备——这是 reset 重建安全的前提，有 `DirtyHal` 测试锁住"返回页必为零"这个后置条件。
- `crates/axdriver_net/`：定义 transport 无关的队列契约和恢复接口。给在途请求引入 `epoch`（`QueueEpoch`）和 `TxCookie`（epoch + ticket），让不关心 VirtIO 细节的上层也能表达所有权；检测到 epoch 耗尽则进 fault。
- `crates/axdriver_virtio/`：`VirtIONetRaw` 适配器，把整设备复位拆成有界步骤：`status` 清零确认后才关闭旧 RX/TX owner、重建队列并 refill，失败时把旧 backing 纳入 quarantine、不提前释放。

VirtIO 原生 reset 会和 `Drop` 一起无限自旋，运行时触发即卡死。设备层把它改成有界的 `reset/config` 原语，reset 触发与"确认 status=0"分离，config generation 变化可重试。

## 队列与固定槽位：有界内存 + 完成账本

`device/fixed_queue.rs` 提供 transport 无关的固定容量槽位存储，收发包共用同一套规则，本机内存有硬性上界：

- 每个槽存一整帧（上限 `MTU 1500 + 14 字节以太头`），启动时一次性分配，之后 datapath 不再分配。
- 同时活跃的 TX ticket 有上界（`MAX_LIVE_TICKETS = 128`）。
- 每个发出的包带 ticket，完成一个回收一个；账本区分正常回收 / 提交前取消 / reset 中止 / 带阶段的故障，对不上就报错并保持原状——避免重复回收和谎报完成。
- 队列满返回"忙"，上层注册 waker 等；腾出空间再继续（背压）。

## 队列 owner：唯一后台任务

收发由**唯一一个队列任务**推进，按固定预算分阶段做，避免任一边饿死：

```
loop {
    reclaim（回收已完成的 TX ticket，还 buffer）
    receive（refill RX 槽？从 used-ring reap 收包 → 交给栈入库）
    submit（把栈/路由下来的帧放出 ethernet）
}
```

- 每个阶段有 budget，耗尽即让出，不 unmask。
- ISR 只唤醒，实际搬数据全在这个任务里——和串口 copier 同一原则。

收房任务还兼任**恢复属主**：设备出错时由它驱动恢复状态机，不另起第二个队列任务、不加轮询兜底（见故障恢复节）。

## ISR → 唤醒机制

中断处理极简，只做三件事：

1. 读 cause，分类为 used-buffer / config-change / 未知 / 虚假中断，并记录计数。
2. ack / mask 对应位。
3. `AtomicWaker::wake()` 唤醒队列任务或栈 runner。

寄存器回看（register-recheck）防丢唤醒：无论事件先到还是登记先到，都不会漏。一批包只唤醒一次，避免高频小包下的中断风暴（中断合并）。`config-change` 作为独立 cause 发布，驱动读一致链路快照——它不搬运 descriptor、不伪造 used-ring 完成，只做信令。

## 栈 runner：协议栈自己推进

`stack_runner.rs` 的常驻任务独立推进 smoltcp 的收进入 / 发输出 / 定时维护（ARP 过期、TCP 重传）。唤醒源三类：设备进度、软件改动、定时器到期。通知用带 generation 计数的 `AtomicWaker` 事件对象，设备进度与软件改动共用同一 generation，与队列 owner 的 epoch 独立。

没有流量时它睡下，不空转；正常路径靠事件唤醒，另留低频兜底覆盖边界情况，不做持续轮询。smoltcp 的入口沿用本地化后 `Service::poll` 的 `poll_ingress_single` / `poll_egress` 模式。

## 就绪桥：多等待者扇出

smoltcp 内部每个 socket 只有一个**单槽一次性 waker**，一次只能唤醒一个等待者。`readiness.rs` 的 `ReadinessBridge` 做逐 handle 展开：

- 每个公共 TCP/UDP handle 持一个共享桥，分成**读、写、终态**三组 `PollSet`。
- smoltcp 的收发 waker 指向桥，由桥把所有已注册的应用等待者扇出唤醒。
- 隐藏的监听 socket 不进公共注册表。

终态用稳定代号编码（对齐共享 `DevError` 的 1~8，`9` 是连接被拒），以原子值保存跨发布存活，保证 poll 之后实际收发拿到的错误与就绪结果对得上——多等待者、handle close/reuse、overflow、poll 后 I/O 一致性都由此保证。

## flush：等一批真正收尾

`flush.rs` 提供"冲刷"：构造时捕获此刻的 `last_accepted` TX ticket 作为 target，future 完成后、等所有 **`<= target`** 的 live ticket 被 reclaim——不依赖完成顺序，也绝不等 target 之后新提交的票。它只表示"驱动缓冲已还原"，不表示已到 wire / peer / TCP ACK / 应用。

## 故障恢复：断在任意一步都不烂尾

异步打通的是正常路径，恢复语义负责"坏了怎么安全收尾"。核心是把**所有权**用 epoch 钉死：

- **epoch 账本**：每次复位推进一个 `QueueEpoch`，在途请求带 `(epoch, ticket)`。只有当前 epoch 的完成才算数；迟到 / 重复的旧 epoch 完成只能记为 stale/fault witness，**绝不命中新 epoch 的对象、不回收新 buffer、不完成新 flush**——防 double-free、防误归属的边界线。
- **分层取消**：三层所有权，处理不同。
  - waiter：清等待记录；
  - queued（在软件队列、未提交）：撤销并返回取消错误；
  - device-owned（已给设备）：只能 quiesce/reset 后回收，普通 `future` 被 drop 不转移所有权、不提前释放内存。
- **阶段 deadline**：提交等待、等完成、回收各自独立的 1 秒绝对截止，超时按原因处理（取消 queued 或进 recovery/fault），并产出一致可读的故障身份（`stage / epoch / cause / owner`），不是分散原子值拼出的撕裂快照。
- **常驻恢复状态机**：唯一 owner 驱动 `Active → Quiescing → Resetting → Reinitializing → (Active / Faulted)`。quiesce 1s、reset 2s、reinit 2s 三段 deadline；每轮只做有界账本工作和至多一个驱动步骤；**成功提交新 QueueEpoch 后才开放 I/O 并唤醒**，失败则保留 Faulted owner 与 backing、任务驻留不退出。
- **链路控制面**：config-change 读到一致 link snapshot。link down 关闭当前 `SocketEpoch`、取消 queued、阻止新入队/提交，但 device-owned 继续回收、不伪造完成；link up 只推进 SocketEpoch、放行新会话，**不推进 QueueEpoch、不自动整机复位**。
- **socket 终态按 epoch 隔离**：旧 epoch socket 恢复后稳定返回稳定错误（reset→`ConnectionReset`，link down→`NotConnected`，timeout→`TimedOut`，cancel→`Interrupted`）；先提交错误再唤醒，恢复后新 socket 不继承旧 terminal、旧 socket 不复活。既有 TCP 连接不透明续传，是明确取舍。

配套还有一个 QEMU 侧的诊断/验证面（`kernel/src/syscall/fs/ctl.rs`）：版本化快照 ioctl（V1–V4，只增不改 ABI）、诊断 hold/lease 用于制造队列停滞、`flush` 与 `reset_request`。

## 验证方式

- **自动层**：单元测试（含 deterministic clock / fake transport / DirtyHal）、host harness、`make host-test`、内核编译。
- **手工层**：单 hart QEMU VirtIO-MMIO 上跑探针，逐条打 `PASS` marker，跑完用只读 validator 对整段串口离线审计，不人工摘抄。回归累进：每层收口重跑此前全部套件。

结论范围限定 single-hart QEMU 的软件/设备模型，不覆盖 SMP、真板 DMA/IRQ 时序与性能。

## 与异步串口的对比

| 维度 | 串口 | 网卡 |
|---|---|---|
| 数据单位 | 字节流 | 整包（帧） |
| 设备侧存储 | 字节 ring | 描述符 ring + DMA |
| 所有权单位 | 字节位置 | (epoch, ticket) + 固定槽位 |
| 后台任务 | 单一 copier | 队列 owner + 栈 runner |
| 唤醒扇出 | 单等待者 | ReadinessBridge 多等待者 |

可迁移的经验：ISR 极简（只唤醒不搬数据）、register-recheck 防丢唤醒、背压（满了让上层等）、分层完成。不能迁移的：字节 ring 布局、单一 copier 任务模型——网卡以描述符和 packet-buffer 所有权为基本单位。

## 参考

- [异步网卡架构探索](async-nic-architecture-exploration.md)：早期分层与方案取舍
- [异步串口驱动总体架构](../异步串口/async-uart-driver-architecture.md)：可对照的已定型异步驱动
- [异步网卡手动测试逐项：MS1 到 MS7 每个 PASS 在验什么](async-nic-manual-tests-per-item.md)：各层验收的具体断言
- [网卡异步化：全链路异步已打通](nic-async-status-device-done-app-pending.md)：各阶段状态
- [smoltcp 接入与 axnet 本地化决策](axnet-localization-decision.md)：本地化与 device 边界