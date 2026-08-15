# W08 - 网卡收包异步化完成，双向收发推进

**周期**：2026-08-09 ~ 2026-08-15
**分支**：`net-k3`（13 个提交）

## 本周做了什么

本周完成网卡收包的异步化改造：收包不再靠定时查看（轮询），改为设备有数据时发出中断、唤醒后台任务去取，空闲时任务睡觉。收包路径跑通后，开始推进收和发两个方向的数据通道：收发包共用固定大小的内存槽位，满了就让上层等着（背压），发完一个回收一个，还能等待一批数据全部真正发出（冲刷）。

我还和我的导员沟通了一下[实习协议情况的说明](../fornow/实习协议相关问题.md)

另外，向老师，我感觉异步网卡的工作量有点超出我的预期，难度也是，这或许有点强一个大三学生所难了，秉持着挑战自己的想法，我会鞭策自己和ai努力完成的。

补助这周到了，很开心，是我第一次靠自己赚到钱，希望以后能赚更多钱的同时也能这样开心。

- [附件4：企业信息介绍](../fornow/附件4：企业信息介绍.docx)
- [实习信息反馈表](../fornow/附件10：实习信息反馈表.docx)
- [青海大学计算机学院实习制度详解](../fornow/青海大学计算机学院实习制度详解.pptx)

## 收包异步化（完成）

**为什么做**：网卡数据到达时，系统不知道，只能每隔一小段时间去问一次。没有数据时这些查看是空转，白白占处理器。改成"设备有数据时通知系统"后，没有数据时后台任务可以睡觉。

**做法**：

- 中断处理只做最少的事：读设备状态、记录次数、唤醒后台任务，不碰数据、不碰队列。
- 收包搬运只由一个后台任务负责，每次最多处理固定数量的包（预算），处理完就睡。
- 用"先登记再检查"的方式堵住唤醒丢失：无论通知先到还是登记先到，都不会漏。
- 一批包只通知一次，减少高频小包下的中断风暴。

**验证**：中断链路、唤醒、并发重复测试全部通过；收包预算可观测；模拟器里的收包验证程序全部通过。阶段收口时补充了运行记录。

## 双向数据面（进行中）

**为什么做**：收包能异步了，发包还是旧的同步路径。要让收发包共用一套规则：内存占用有上界，槽位满了能感知并恢复，缓冲区不泄漏，发完有明确通知。

**做法**：

- 固定容量槽位：收发包各 64 个、每个最大 1514 字节，启动时一次性分配，内存有上界。
- 收发一体后台任务：每轮先回收、再收、再发，三个阶段各有预算，互不饿死。
- 完成账本：每个发出去的包有编号，完成一个回收一个，一一对应；对不上就报错并保持原状。
- 有目标的冲刷：提交一批后可以等待这一批全部真正发完，而不是"塞进队列就算成功"。
- 状态快照与诊断控制：内部账本可见，可暂停/放行某一阶段，用来复现"槽满"场景。
- 验证程序：固定判定标准，能证明真的发了指定数量的数据、在截止时间内完成。

**当前状态**：主体代码和自动验证完成 22/25 项。收尾还剩两步：自动验证的记录标准连续两轮评审未达要求，需要重新设计验证方式后再继续；之后在模拟器里手工运行真实收发场景（这一步需要普通终端，当前环境不允许打开网络端口），再做最终核对。

## 提交记录

| 日期 | 提交 | 内容 |
|---|---|---|
| 8/10 | [`917b40d1`](https://github.com/daivy2333/StarryOS/commit/917b40d1) | 搭建本地网卡驱动组件框架 |
| 8/10 | [`661f6fcd`](https://github.com/daivy2333/StarryOS/commit/661f6fcd) | 网卡设备抽象与测试调整 |
| 8/11 | [`79ea1f9d`](https://github.com/daivy2333/StarryOS/commit/79ea1f9d) | 异步收包核心逻辑（唤醒 + 收包任务） |
| 8/11 | [`e0fac50c`](https://github.com/daivy2333/StarryOS/commit/e0fac50c) | 中断接入与诊断处理 |
| 8/12 | [`78e1f7ab`](https://github.com/daivy2333/StarryOS/commit/78e1f7ab) | 收包验证程序与刺激脚本 |
| 8/12 | [`8f5b5228`](https://github.com/daivy2333/StarryOS/commit/8f5b5228) | 队列通知控制调整 |
| 8/12 | [`3e181464`](https://github.com/daivy2333/StarryOS/commit/3e181464) | 收包阶段收口，补充运行记录 |
| 8/13 | [`1a2bc99f`](https://github.com/daivy2333/StarryOS/commit/1a2bc99f) | 双向改造方案与队列契约设计 |
| 8/13 | [`5d1a2268`](https://github.com/daivy2333/StarryOS/commit/5d1a2268) | 固定槽位与驱动适配测试 |
| 8/14 | [`244803fb`](https://github.com/daivy2333/StarryOS/commit/244803fb) | 双向后台服务与路由调整 |
| 8/14 | [`e1fde918`](https://github.com/daivy2333/StarryOS/commit/e1fde918) | 故障处理顺序与并发测试隔离 |
| 8/15 | [`223f6281`](https://github.com/daivy2333/StarryOS/commit/223f6281) | 冲刷、状态快照与验证程序 |
| 8/15 | [`8dc3ef7d`](https://github.com/daivy2333/StarryOS/commit/8dc3ef7d) | 诊断控制收口与自动验证 |

## 下周

- 重新设计自动验证的记录方式，把记录标准收口
- 在模拟器里手工运行真实收发场景，补齐剩余验收

## 参考

- [网卡收发的异步化：为什么做、怎么做](../notes/异步网卡/async-nic-rx-tx-why-how.md)
- [异步网卡架构探索](../notes/异步网卡/async-nic-architecture-exploration.md)
- [网卡性能测试矩阵：测什么、为什么这么测](../notes/异步网卡/nic-benchmark-matrix-research.md)
- [收包异步化的运行记录](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/ms04-qemu-async-rx-core-evidence.md)
- [异步收包核心逻辑](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/async_rx.rs)
- [后台服务与唤醒](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/service.rs)
- [固定槽位队列](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/device/fixed_queue.rs)
- [冲刷](https://github.com/daivy2333/StarryOS/blob/net-k3/crates/axnet/src/flush.rs)
