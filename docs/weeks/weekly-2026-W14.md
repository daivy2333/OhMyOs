# W14 - UART 与网卡固定绑核，唤醒路径改为本地优先

**周期**：2026-09-19 ~ 2026-09-26
**仓库**：StarryOS（`mul-hart-k3` 分支，本周 1 个提交）

> W13 定了多 hart 适配方案，第一阶段（同步与调度基础）在 9/17 提交。本周把方案往下推一层：UART 和网卡的后台角色都固定到指定 hart，并补上能直接看到"谁跑在哪个 hart"的观测命令。过程中修了三个真实缺陷：UART TX 丢唤醒、release 构建越界读出幽灵 hart、唤醒路径不通知远端。9/21 提交的是 WIP 快照（45 个文件，+12293/-178），工作区还有未提交的收尾改动，正在继续推动工作，下周结束之前应该能做完吧。

## 固定绑核：角色到 hart 的分配变成显式规则

四个后台角色：UART RX copier、UART TX copier、网卡队列 owner、网卡协议栈 runner。

分配规则集中在 `kernel/src/drivers/placement.rs`：从已发布的可调度 hart 集合里取，角色 i 落在 `schedulable[(anchor_pos + i) % len]`。SMP=16 下四个角色落在不同 hart 上，只有单 hart 才共置。集合为空或非法直接失败，不去选未初始化的运行队列。掩码容量取 `axconfig::plat::MAX_CPU_NUM`，不再硬编码。

copier 和 runner 都在副 hart 就绪之后才启动，且是入队前绑核，各启动一次。

## 观测：UART 152 字节快照与网卡 V5

UART 新增 QEMU-only 快照命令 `0x55534d31`，152 字节定长帧，逐字节序列化。直接 memcpy 结构体会把 `repr(C)` 填充的未定义字节带进用户态，所以走的是序列化后的字节数组。

帧内包含：配置/在线掩码、RX/TX copier 绑核、实际中断 hart、copier 最近与累计 hart、ring 占用与空位、TX 四阶段完成、远端入队/IPI/恢复计数、非法绑核拒绝数。旧 TXDBG 接口不变。

网卡 V5 快照命令 `0x4e495435` 按字节是 V4 的前缀扩展，V1–V4 的命令、布局、语义不动。区别在观测方式：V5 里 owner 和 runner 的 hart 是任务在轮询入口记下的实际值，不再从绑核掩码推断。

## 唤醒路径：本地优先 + 单次 IPI

现象：UART TX copier 固定在远端 hart，startup 打完一个字节后 copier 最终恢复了，但 IPI 发送和接收计数一直是 0。原因是 waker 复用了普通 spawn 的全局轮转来选目标，并以 `resched=false` 入队，远端入队不发 IPI。

把唤醒一律改成 `resched=true` 的实验被完整回退：全掩码的网络协议栈 runner 会在 16 个 hart 之间被唤醒轮转，形成 IPI 风暴。

最终做法是把唤醒目标选择从 spawn 里拆出来。`crates/axtask/src/run_queue.rs` 新增纯函数 `select_wake_cpu`：当前 hart 同时在任务亲和集与可调度集内就留在本地，否则从两者交集里确定性地取一个，交集为空则失败。`AxWaker` 走这个入口并请求真实的 `Blocked → Ready`，本地只置抢占标志，远端恰好发一次 IPI，重复唤醒不发。普通 spawn 仍走原来的轮转。

顺带修掉 UART TX 丢唤醒：copier 注册 ring waker 后重查 ring，发现新数据就 self-wake 重试，不直接停放，并新增 `park_after_register_retry` 计数。这个竞态之前表现为同一个二进制时而 PASS 时而失败。

上面四块的完整做法和代码位置写在 [多 hart 适配是怎么落地的](../notes/异步驱动/multi-hart-adaptation-solution.md)。

## release 越界读出幽灵 hart

绑核代码曾按硬编码 64 遍历 `CpuMask<16>`，而 `cpumask 0.1.0` 的索引 `get`/`set` 只有 `debug_assert`。release 下越界读返回垃圾 true，于是并不存在的 hart 16 被当成可调度。

修法是在 `crates/axtask/src/cpumask.rs` 加自有类型 `AxCpuMask` 包装 registry 类型，索引访问在 debug 和 release 都做容量检查：越界读报"不在集合"，越界写返回错误且不改动掩码。registry 类型不再从公开 API 露出，没有 `Deref`、`AsRef`，也拿不到内部值。

## 迁移观测：控制命令已提交，读写序仍缺一块

已提交：两个 QEMU-only 控制命令，把 copier 和 owner/runner 的绑核从单 hart 扩到两个 hart，观察任务自然迁移后再恢复固定绑核；目标非法时失败并保留原掩码。迁移视图用原子字段加 seqlock 发布。

未完成：提交里的 seqlock 两侧序关系仍有缺口。写侧在奇数标记后加 Release fence，但 Release 只约束它之前的操作，后续字段写仍可能越过；读侧在最终序号检查前也少一道边界。弱内存序下读者可能接受混合视图。修法已写成方案，代码还没改。

## 验证状态

- UART 阶段的提交记录：52/52 宿主测试、65/65 UART 测试通过，普通 / SMP=16 / D1 构建通过。
- 网卡阶段与迁移阶段目前只到宿主状态机与构建层。SMP=16 上的分驱动运行验证（串口单独一遍、网卡单独一遍，各自独立判定）和两驱动组合压力还没做。
- 工作区未提交：迁移视图读写序修正、SMP=16 分驱动验证的记录（pcap、guest 日志）、网卡对端脚本和文档归档。这些还没做完，不计入本周成果。

## 提交记录

| 日期 | 提交 | 内容 |
|---|---|---|
| 9/21 | [`9efb2529`](https://github.com/daivy2333/StarryOS/commit/9efb2529) | 共享绑核策略、UART copier 与网卡 owner/runner 固定绑核、UART 152 字节快照与网卡 V5、唤醒路径本地优先 + 单次远端 IPI、`AxCpuMask` 容量安全、迁移控制命令与观测底座 |
| 9/17 | [`6796315a`](https://github.com/daivy2333/StarryOS/commit/6796315a) | 同步与调度基础：全局临界区、工作区 `axtask` 副本与入队前绑核、远端就绪 IPI、PLIC 窗口修正 |

## 下周

- 和大家一样完成训练营工作收尾
- 把qemu上面的工作，多hart情况下的异步串口和网卡跑通
- 然后就是继续配置k3的开发环境

## 参考

- [笔记：多 hart 适配是怎么落地的](../notes/异步驱动/multi-hart-adaptation-solution.md) — 临界区、每 hart 运行队列、远端 IPI、角色绑核分配的具体做法与代码位置
- [笔记：为什么异步驱动要做多 hart 适配](../notes/异步驱动/multi-hart-adaptation-why-and-how.md) — 本周工作针对的问题清单
- [笔记：内存序 —— QEMU 掩盖的真板陷阱](../notes/异步串口/memory-ordering-smp.md)
- [笔记：SMP —— 核心数量如何影响调度，多核为什么不能只靠调度器](../notes/学习内容/smp-multicore-what-we-are-looking-at.md)
- [StarryOS（daivy2333/StarryOS）](https://github.com/daivy2333/StarryOS) — 本周工作在 `mul-hart-k3` 分支
- [K3 资料仓（daivy2333/k3）](https://github.com/daivy2333/k3)
