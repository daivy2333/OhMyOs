# W11 - 故障恢复语义：设备出错到链路断开都能安全恢复

**周期**：2026-08-28 ~ 2026-09-04
**分支**：`net-k3`（本周 6 个提交）

## 本周做了什么

上周把设备侧数据面收口、并把 socket 就绪状态桥到 select/poll，但那套东西只保证"正常运行"。一旦设备在 reset 中途出错、或者链路断开，旧实现是 boot 全周期 fail-stop：迟到的完成信号会被误归属到新对象、缓冲区可能被重复回收、等待者永久挂起、或静默丢包。这周把**故障恢复语义**闭环了——在单 hart QEMU 上对设备 reset、分层取消、阶段超时和链路断开/恢复都做了确定性处理，跑完六个场景并重验了此前四组既有回归，全部通过。

## 设备层：把 reset 从"无限等待"改成一步步有界恢复

VirtIO 原生的 reset 会和 Drop 一起无限自旋——运行时一旦触发就会卡死。先给 transport 层加了有界的 reset/config 原语：每次调用只做有界寄存器操作，reset 发起和"确认回到 status=0"分开；config generation 变化时返回可重试结果，运行时 reset 失败不会触发旧队列/backing 的 Drop。随后定义 transport 无关的恢复契约给驱动层暴露，socket cookie 带上世代（epoch），世代耗尽直接进 fault。接着在 VirtIO 适配器实现整设备 recovery：分步状态机，确认 status=0 后关闭旧收发队列、重建设备并重新填充收包槽；任一步失败就保留 faulted owner / backing、拒绝新提交，不提前释放内存。

- [DMA 分配与零页契约（hal.rs）](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/virtio-drivers/src/hal.rs)

## 数据面：分层取消 + 每阶段独立截止时间

把"包到底怎么结束的"用一个固定账本钉死：正常回收、提交前取消、reset 中止、带阶段的故障，四种结果分开记账，不再出现"删了就被 flush 当成完成"的含糊。取消按三层处理：只清在等的 waiter；仍在软件队列的包取消并返回错误；已被设备拿走的包（device-owned）只能等 quiesce/reset 清，普通 future 被 drop 不回收它的缓冲区。给 submit、completion、reclaim 各自设独立的 1 秒绝对截止时间，不再用一个笼统的阶段标签代替计时器；超时按原因处理，并产出一个跨发布一致、可整体读取的故障身份（stage + cause + 世代 + owner 汇总），不是分散原子变量拼出来的撕裂快照。

## 常驻恢复：一个任务驱动五态状态机

恢复逻辑放进唯一的常驻后台任务里，驱动 `Active → Quiescing → Resetting → Reinitializing → Active/Faulted` 状态机。quiesce 1 秒、reset 2 秒、reinit 2 秒三段截止，每轮只做有界账本工作和至多一个驱动步骤；成功提交新世代后才重新开放收发并唤醒等待者，失败则保留 faulted owner 和 backing，任务原地驻留不退出。没有新建第二队列任务，也没有加轮询兜底。

## 链路控制面：断开不伪造完成，恢复不复活老连接

config-change 中断作为独立 cause 发布到任务上下文，驱动读到一致的链路快照。链路断开时：关闭当前 socket 世代、取消仍在队列里的包、阻止新入队，但已被设备拿走的包继续回收；链路恢复只推进 socket 世代、放行新会话，不重置队列世代。配置中断只做信令，不搬运 descriptor、不伪造 used-ring 完成。

## socket 终端错误：按世代隔离，旧连接不再透明续传

之前 socket 的错误是 boot 全局"先到先得"，晚创建的句柄也会继承。这周改成按世代隔离的终端语义：reset 前创建的旧 socket 在恢复后仍返回稳定的终止错误（连接被重置），链路断开时新发送返回未连接，超时映射为超时，取消映射为被打断——先提交错误再唤醒；恢复后只有新 socket 可用，旧 socket 不复活。代价是既有的 TCP 连接不会透明续传，这是明确接受的取舍。

- [socket 终端与就绪桥（readiness.rs）](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/readiness.rs)

## QEMU 资格与两个产品 bug 修复

自动验证加了一套只对 QEMU 开放的 recovery 控制/快照 ioctl、guest 探针和纯输出校验器（校验器只审计、不吃数据、不启动 QEMU），复用现有诊断通道制造队列停滞来触发 reset 场景。手工跑 QEMU 过程中揪出两个产品 bug 并修掉：

- **poll 空集合超时**：RISC-V 上 `poll(NULL, 0, t)` 实际走 `ppoll`，后者对零 `nfds` 的 NULL 指针返回 EFAULT，运行程序卡在了 reset 前的等待。改成 `nfds==0` 时忽略 `fds`、把安全的空切片交给现有定时唤醒路径；`nfds>0` 仍校验用户地址。
- **重建队列读到陈旧环**：`Dma::new` 分配 DMA 页后没清零，重建队列会读到复用页里陈旧的 used-ring 索引，把过期 token 当成完成（token 28526 越界崩溃）。在返回前把整段区域清零，并加了一个"脏内存"测试（分配后先填非零 pattern）先证明问题再证明修复，覆盖所有方向以及 modern/legacy 两种队列布局。

修完后手工在单 hart QEMU VirtIO-MMIO 跑完整六个场景：reset 前流量、队列停滞→恢复、旧 socket 终止、新世代双向流量、HMP 关/开链路——按序全部通过，运行程序以退出码 0 结束。四组既有回归也全过：异步收包、双向数据面、应用可见异步网络，外加早先的设备基础核算，逐项 PASS。唯一小插曲是 QEMU monitor 的 `(qemu) ` 提示符污染了链路关闭时采集的一行标记，严格校验器因此对关闭这一步报退出码 1，判断是采集竞态而非产品问题，按你的意思豁免计入通过。

## 提交记录

| 日期 | 提交 | 内容 |
|---|---|---|
| 8/28 | [`aab92f95`](https://github.com/daivy2333/StarryOS/commit/aab92f95) | 打开需求、方案、规格与任务拆解 |
| 8/28 | [`2a303eaa`](https://github.com/daivy2333/StarryOS/commit/2a303eaa) | transport 有界 reset/config、驱动恢复契约、账本与分层取消底座 |
| 8/29 | [`596b324b`](https://github.com/daivy2333/StarryOS/commit/596b324b) | 数据阶段独立截止时间与一致故障身份 |
| 8/30 | [`05528313`](https://github.com/daivy2333/StarryOS/commit/05528313) | 常驻恢复状态机、链路控制面、socket 世代与探针/校验工具 |
| 8/31 | [`b83e800a`](https://github.com/daivy2333/StarryOS/commit/b83e800a) | 探针/校验器收口与清理旧验证工具 |
| 9/4 | [`13ae5a6a`](https://github.com/daivy2333/StarryOS/commit/13ae5a6a) | DMA 清零、poll 空集合修复，QEMU 六场景与四组回归证据 |

## 后续工作

- 板子已经到手了，还在做探索工作，之前没用过这么贵的板子得好好看看，别不小心整坏了。
- 后面还有六周的时间，保守估计，应该都会耗在k3板子上面，本来还想帮帮王政雄，然后有三个异步驱动了就可以试着总结相似点，归纳一个异步开发的图景，以后再说吧，先把我自己的东西跑起来更重要。

## 参考

- [网卡异步化：全链路异步已打通（含故障恢复闭环）](../notes/异步网卡/nic-async-status-device-done-app-pending.md)
- [异步网卡驱动总体架构](../notes/异步网卡/async-nic-driver-architecture.md)
- [网卡收发的异步化：为什么做、怎么做](../notes/异步网卡/async-nic-rx-tx-why-how.md)
- [异步网卡的手动 QEMU 测试：MS1 到 MS7 在测什么](../notes/异步网卡/async-nic-manual-qemu-acceptance.md)
- [异步网卡手动测试逐项：每个 PASS 在验什么](../notes/异步网卡/async-nic-manual-tests-per-item.md)
- [恢复状态机与数据阶段截止（async_rx.rs）](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/async_rx.rs)
- [分层取消与取消/提交线性化（fixed_queue.rs）](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/device/fixed_queue.rs)
- [QEMU 校验脚本（ms07-qemu-validate.py）](https://github.com/daivy2333/StarryOS/blob/net-k3/scripts/ms07-qemu-validate.py)

**采集记录（单 hart QEMU 手工验收，`net-k3` 分支）**：

- [六场景原始串口日志（qemu-serial.log）](https://github.com/daivy2333/StarryOS/blob/net-k3/openspec/changes/archive/2026-09-02-ms07-qemu-single-hart-recovery-semantics/evidence/007-single-hart-qemu-qualification/006-rework/qemu-serial.log)
- [四组回归终态汇总（regressions.txt）](https://github.com/daivy2333/StarryOS/blob/net-k3/openspec/changes/archive/2026-09-02-ms07-qemu-single-hart-recovery-semantics/evidence/007-single-hart-qemu-qualification/006-rework/regressions.txt)
- [手工验收操作手册（含 HMP 关/开链路步骤）](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/ms07-qemu-single-hart-recovery-evidence.md)