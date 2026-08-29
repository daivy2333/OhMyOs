# W10 - 应用可见的异步网络：栈自己跑，就绪状态桥到 select/poll

**周期**：2026-08-23 ~ 2026-08-29
**分支**：`net-k3`（10 个提交）

## 本周做了什么

设备侧的工具（队列、槽位、背压、完成账本、冲刷）上周已经收口，这周让应用层真正"看得见"异步网络。两件事：让协议栈自己跑一个后台任务推进收发和维护，不再靠上层主动调；把 socket 的就绪状态从单槽唤醒接到系统的 select/poll 上，多个应用等各自的 socket 都能被正确唤醒。过程中还排查掉一个 host 单元测试的并行竞争，最后完成 QEMU 手工验收并归档。

## 协议栈独立推进：常驻 runner

协议栈的收包、发包、ARP 过期、TCP 重传这类事情，原来依赖上层每次操作时顺带推进；没流量时上层在线程里空转等待。这周加了一个常驻后台任务（`axnet-stack-runner`）自己维护收发和维护循环：设备中断、软件改动、定时器到期都能叫醒它，空闲时睡下去，不轮询。

通知用带 generation 计数和 `AtomicWaker` 的事件对象做成，设备进度和软件改动共用同一个 generation。它和 `async_rx` 里队列属主（queue owner）的 generation 分开——设备进度唤醒栈推进，但不改变队列所有权。另留了 10ms 兜底轮询处理边界情况，正常路径靠中断、软件事件和定时器唤醒。

- [栈 runner 通知与生命周期](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/stack_runner.rs)

## 就绪状态桥到 select/poll

smoltcp 的 socket 内部只有单槽的一次性收发 waker，一个 socket 只能被一个等待者唤醒。这周加了一层 ReadinessBridge 把它逐个 public handle 展开：每个 TCP/UDP handle 各持一个共享桥，分成读、写、终态三组 PollSet；smoltcp 的收发 waker 指向桥，由桥把所有已注册的等待者都扇出唤醒。隐藏的监听 socket 不进公共注册表。

终态用稳定的 1~9 代号编码（对应 `DevError`，9 是连接被拒），以原子值保存跨发布存活，保证轮询之后实际收发拿到的错误和就绪结果对得上。

- [就绪桥：per-handle 扇出](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/readiness.rs)

## host 单元测试的并行竞争排查

验证时发现 host 测试偶发假 RED：并行跑共享全局 socket 状态的测例时好时坏，出现单测失败甚至 SIGSEGV/SIGABRT 进程崩溃。用对照组二分和字节级换回旧版验证后确认，这个竞争早于本步骤引入，与本步骤的产品改动无关，是既有测试基础设施的共享全局状态竞争。处理方式是给 axnet 的 host 测试做进程内隔离，让并行结果与确定性口径互相印证。

- [竞争归因记录（Incident）](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/incidents/2026-08-26-parallel-global-socketset-test-race.md)

## 验收与归档

QEMU 手工验收全过；回归方面本步骤 12/12、此前协议栈基线 14/14、异步接收基线 4/4、设备双向数据面六种模式全部 PASS。作为收尾记录，补了 QEMU 证据抓取的操作手册和一段校验脚本。

- [QEMU 证据抓取（操作手册）](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/qemu-evidence-capture.md)
- [QEMU 校验脚本](https://github.com/daivy2333/StarryOS/blob/net-k3/scripts/ms06-qemu-validate.py)

## 提交记录

| 日期 | 提交 | 内容 |
|---|---|---|
| 8/22 | [`2079bb96`](https://github.com/daivy2333/StarryOS/commit/2079bb96) | 打开变更：方案、规格、任务拆解（计划文档） |
| 8/23 | [`fb87c8d3`](https://github.com/daivy2333/StarryOS/commit/fb87c8d3) | 常驻栈 runner：通知、生命周期、时钟与首轮骨架，kernel 接线 |
| 8/24 | [`0acc0813`](https://github.com/daivy2333/StarryOS/commit/0acc0813) | 就绪桥与 TCP/UDP/wrapper 接入，回填协议栈基线测试 |
| 8/25 | [`fdc8f101`](https://github.com/daivy2333/StarryOS/commit/fdc8f101) | 栈 runner 收敛（监听表/服务/runner） |
| 8/25 | [`4396d264`](https://github.com/daivy2333/StarryOS/commit/4396d264) | 栈服务推进与 UDP 排空 |
| 8/26 | [`1ea51427`](https://github.com/daivy2333/StarryOS/commit/1ea51427) | 监听 accept 唤醒排空与基线兼容 |
| 8/27 | [`b1e24888`](https://github.com/daivy2333/StarryOS/commit/b1e24888) | 就绪桥完整接线 + 探针测试 + 校验脚本 |
| 8/27 | [`832abfea`](https://github.com/daivy2333/StarryOS/commit/832abfea) | 诊断与 socket 终态语义收敛 |
| 8/27 | [`1d0313ad`](https://github.com/daivy2333/StarryOS/commit/1d0313ad) | 设备数据面探针修正 |
| 8/27 | [`9d58bd42`](https://github.com/daivy2333/StarryOS/commit/9d58bd42) | 验收收口：校验脚本与探针定稿 |

## 下周（MS7）

- 做故障恢复语义：在单 hart QEMU 下闭环设备 reset、分层取消、分阶段超时和链路断开/恢复（link flap）时异步对象的生命周期，保证不会出现悬垂引用、重复回收、永久挂起或静默丢包。
- 另外，k3板子到了，虽然当前工作重心还在qemu，但是还是打算做一些探索性工作，我当前打算把qemu上的工作做完再转向上板试验。

## 参考

- [网卡异步化：全链路异步已打通（设备侧与应用侧现状，含 MS7 故障恢复说明）](../notes/异步网卡/nic-async-status-device-done-app-pending.md)
- [网卡收发的异步化：为什么做、怎么做](../notes/异步网卡/async-nic-rx-tx-why-how.md)
- [异步开发路径：骨架层可复用，肉层是设备特定的](../notes/异步驱动/async-device-skeleton-reusability.md)