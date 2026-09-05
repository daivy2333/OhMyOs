# 异步网卡手动测试逐项：MS1 到 MS7 每个 PASS 在验什么

**日期**：2026-09-05
**标签**：rust, async, network, nic, qemu, test, acceptance

> 配套 [异步网卡的手动 QEMU 测试：MS1 到 MS7 在测什么](async-nic-manual-qemu-acceptance.md)。那里讲每套测试的范围，这里一行行拆开——那些 `PASS / MS0x PASS mode=…` 到底通过的是什么断言。信息来自 guest 探针源码（`tests/ms0x_*.c`）与各 runbook 的判据。

## MS1 — socket 基线（14 个 PASS）

探针 `tests/ms01_socket_baseline.c`，在 guest 里靠 loopback（127.0.0.1）自测，检验协议栈本地化之后基础 socket 行为。14 个 `PASS:` 分别是：

| PASS 标记 | 验什么 |
|---|---|
| `tcp-accept` | TCP bind/listen/accept 一条连接，客端发 `tcp-ms01`、服务端完整收到，且在期限（15s）内完成 |
| `tcp-adjacent` | 两个相邻 TCP 连接各自独立：两个客端各发 A/B，服务端 accept 到两条不同连接且都读对 |
| `tcp-512cap: accepted 512 of 512` | `listen` 队列容量设为 512，连续建满 512 条连接全部 accept 成功——容量上界不丢 |
| `tcp-512-recovery` | 512 已满后关闭其中一条，一条新连接能恢复建立——满了能腾出来再连 |
| `tcp-relisten` | 服务端 close 后能对同一端口重新 bind+listen，新连接再通——端口可释放重绑 |
| `udp-bidi` | UDP 双向：发 `udp-ms01`，对端 echo 回 `echo-udp-ms01`，匹配 |
| `tcp-nonblock-accept` | 非阻塞 `accept` 无可接受连接时返回 `EAGAIN/EWOULDBLOCK`，不误报 |
| `udp-nonblock` | 非阻塞 `recvfrom` 无数据时返回 `EAGAIN` |
| `poll-readiness` | `poll` 在连接到达前超时不误报 `POLLIN`；连接后返回 `POLLIN` 且 `accept` 可得——就绪状态和实际一致 |
| `udp-source: <ip>:<port>` | UDP `recvfrom` 的来源地址是发送方（127.0.0.1）且端口非零——源地址可见 |
| `bind-getsockname: port` | `bind` 指定端口后 `getsockname` 读回同一端口 |
| `bind-ephemeral: port` | 客户端 `connect` 后 `getsockname` 拿到非零临时端口（内核真的分配了） |
| `bind-conflict: EADDRINUSE` | 同一地址第二次 `bind` 返回 `EADDRINUSE`——端口冲突检查 |
| `bind-close-cleanup` | 关闭后再 bind 同一端口成功——close 后端口真正释放 |

## MS2 — 轮询基线（TCP/UDP + ARP/ICMP + 空闲 CPU）

当时还是同步轮询。`tests/ms02_guest_service.c` 在端口 5555 上用一个 `poll()` 循环同时管 TCP listener、活动 TCP 连接和 UDP socket。

- `MS02_TCP_PASS connection=N`：TCP 完成 **2 次**请求/响应往返（`MS02_TCP_REQUEST`/`RESPONSE`）。
- `MS02_UDP_PASS datagrams=1`：UDP 完成 **1 个**报文往返（`MS02_UDP_REQUEST`/`RESPONSE`）。

另有三个手工检查项：user-net 走 TCP/UDP 5555 端到端、TAP 上 ARP/ICMP 可通、空闲时 CPU 无异常。整体证明同步轮询这条传输路径通了，为中断化打底。

## MS3 — 中断诊断（每模式一个 PASS）

探针 `tests/ms03_irq_probe.c`。中断还没进异步，这步把 IRQ 控制面钉清楚：NET IRQ 7 与 UART IRQ 10 都注册、能分类、能 ack/rearm、互不干扰。每个 mode 一个 `PASS <mode>`：

| 模式 | 验什么 |
|---|---|
| `rx2` | 从 host servant 收 2 个 TCP 包，net used-ring 进度正确、标记齐全 |
| `tx2` | 向 host servant 发 2 个 TCP 包并送达 |
| `uart` | 仅 UART 刺激：net used-ring 的增量保持为 0，net IRQ 不被 UART 活动带偏 |
| `both` | UART 与 net 并发：两个 IRQ 各自正确推进，互不吞事件 |
| `idle` | 有界空闲窗口内无 IRQ 风暴，总增量受控 |

## MS4 — 异步收包（snapshot / idle / nudge / burst）

探针 `tests/ms04_rx_probe.c`，burst 配合 host stimulus（`scripts/ms04_rx_stimulus.py`，端口 15556）。每个 mode 一个 `MS04 PASS mode=<mode>`。这四种模式都靠读 V3 快照观察驱动内部状态来判：快照是网卡驱动借诊断 ioctl 导出的固定大小状态块（72×u64 = 576 字节），探针取前后两次读值的差（delta）断言行为——delta 全为 0 代表空闲时什么都没做，等于 96 代表连收 96 包时描述符进出守恒。

| 模式 | 验什么 |
|---|---|
| `snapshot` | 读到的 V3 快照一致可写：owner/lifecycle 稳定，fault/restore/irq_entry 增量全为 0 |
| `idle` | 静置窗口内 IRQ、软件事件、task、descriptor、budget 的 delta 全为 0——空闲真的在睡，没有隐藏的忙轮询 |
| `nudge` | 一次软件事件恰好唤醒 `task=1`、`nudge=1`、`empty=1`，且 descriptor 增量 0——事件驱动唤醒、不搬数据 |
| `burst` | 连收 96 包：`reaped == refilled == delivered == 96`，`isr_publish`/`isr_wake` 都推进，`budget_exhausted>0`、`self_yield>0`、`fault=0`——有界吞吐、descriptor 回收守恒、预算与自让位都发生 |

## MS5 — 有界双向数据面（六个 mode）

探针 `tests/ms05_data_plane_probe.c` + host stimulus（`scripts/ms05_data_plane_stimulus.py`，端口 15557）。六 mode，每 mode 一个 `MS05 PASS mode=…`，且都要求 `fault=0`、无 panic。数据面走 UDP：guest 作为客户端出站连 host。除 snapshot 外的模式也读同一个 V3 快照（见 MS4）取前后差，用账本是否闭合来判"满→恢复"。

| 模式 | 验什么 |
|---|---|
| `snapshot` | V3 诊断快照（同 MS4）一致可读，fault 增量 0，无诊断 hold 残留（`hold_mode==0`） |
| `tx-only <count> <payload>` | 只发 96 包；TX 账本闭合、尾声闭合（槽位占用归零、无 live/queued/device-owned）、host 计数 `host_received=96` 对得上——发出去了 |
| `bidirectional <count> <payload>` | 双向各收发 96 包，同样账本/尾声闭合——收发两方向都通 |
| `slot-full` | 用诊断 hold 把 TX 槽位占满触发 Recovery，`held→full` 证明真的满，随后帐本完全闭合——满了能感知并恢复 |
| `descriptor-full` | 把 driver/descriptor 占满触发 Recovery，同样证明与闭合——描述符层满也能恢复 |
| `flush` | 冲刷：恰好 1 次 `flush_ok=1`、`fault=0`、账本闭合——等一批真正发完才返回，不把入队当成功 |

## MS6 — 应用可见异步网络（12 个 case）

探针 `tests/ms06_stack_readiness_probe.c`。12 个 case 固定顺序、各恰好一次 `PASS: <name>`，每个在独立期限内完成，且要求约定的事件 bit 全部出现、禁止的 bit 不出现；跑完后同一个 guest session 再跑 MS1/4/5 回归。validator（`scripts/ms06-qemu-validate.py`）对完整串口离线判定。

| case | 验什么 |
|---|---|
| `tcp-timer` | TCP 定时器到期能驱动唤醒——不靠外部数据也按时醒来 |
| `udp-progress` | UDP 数据到达驱动就绪进度 |
| `listener` | 监听 socket 自己的就绪状态可被 poll 到 |
| `nonblock-connect-error` | 非阻塞 connect 失败正确返回错误，不误报为可写就绪 |
| `quiet` | 无活动时不多报就绪——安静路径不虚警 |
| `continuous-traffic` | 持续双向流量下两极都不饿死 |
| `close-error` | close 之后对该 socket 的 I/O 返回正确错误 |
| `poll-multiwaiter` | 同一个 socket 的多个 poll 等待者都被正确唤醒 |
| `select-multiwaiter` | 同上，select 接口 |
| `epoll-multiwaiter` | 同上，epoll 接口 |
| `waiter-64` | 高并发等待者（64 个）全部都能就绪 |
| `waiter-65-reregister` | 超过单批上限（65 个）并注册重试，能重新唤醒——等待者容量与 re-register 语义 |

## MS7 — 故障恢复（6 case + preflight）

探针 `tests/ms07_recovery_probe.c` + host peer（`scripts/ms07-recovery-peer.py`，端口 15572，不加 hostfwd）+ QEMU HMP `set_link net0 off/on`。恢复探针判状态靠 V4 快照——它在 V3 完整状态块之上追加了恢复语义块：当前世代（queue/socket epoch）、链路状态、owner 账本（available/device_owned/quarantined）与故障身份，用来证明 reset 后世代推进、owner 恢复成 64/64/0、链路是 up 还是 down、故障落在哪个阶段。6 case 按固定顺序，每个要求各自 marker 齐全（`MS07_V4` 快照、`MS07_SOCKET`、`MS07_PEER`、`MS07_RESET`、`MS07_HMP_READY`/`MS07_HMP_OBSERVED`），validator（`scripts/ms07-qemu-validate.py`）判终态。进入首个 case 前先跑 `zero_fd_poll_preflight` 验证 poll 空集合四个边界（零超时、有限超时、零 `nfds` 忽略无效地址、正 `nfds` NULL→`EFAULT`）。

| case | 验什么 |
|---|---|
| `pre_reset_traffic` | 恢复前流量正常：与 host peer 完成一次 UDP 往返 |
| `reset_request` | 触发整设备 reset：V4 快照进入 reset、owner ledger 恢复 64/64/0，回到 Active |
| `old_socket_terminal` | 复位前的旧 socket 在恢复后返回 `ECONNRESET`（terminal=reset），不复活 |
| `new_epoch_traffic` | 新时代的新 socket 与 peer 双向收发成功 |
| `hmp_link_down` | HMP `set_link off`：快照 link=down，新发送返回 `ENOTCONN`，不伪造完成 |
| `hmp_link_up` | HMP `set_link on`：link=up，新 socket 双向恢复；QueueEpoch 不推进、SocketEpoch 推进 |

## 一串 PASS 合起来意味着什么

每个 `PASS` 都是"内核行为在当前 QEMU 现场里逐字复现"的最小证据。它们不是同一件事反复测，而是各自钉住一层：MS1 证基础 socket；MS3 证中断是对的；MS4 证收包真靠事件驱动；MS5 证收发在固定槽位下有界且满则恢复；MS6 证应用真的能被主动唤醒；MS7 证断了也能安全还原。最后一层跑通时，会把前面几套（MS1/4/5/6）原样再跑一遍做累积回归——所以 MS7 的日志里能看到前面所有 suite 的 PASS 全数重现。

所有场景细节都来自 `tests/ms0x_*.c` 源码与各 runbook 的判据表；端口号、case 名、通过数可对照原仓库核对。

## 参考

- [异步网卡的手动 QEMU 测试：MS1 到 MS7 在测什么](async-nic-manual-qemu-acceptance.md)：每套测试的范围与演进主线
- [网卡异步化：全链路异步已打通](nic-async-status-device-done-app-pending.md)：各阶段状态与机制
- [异步网卡架构探索](async-nic-architecture-exploration.md)：整体分层与方案取舍