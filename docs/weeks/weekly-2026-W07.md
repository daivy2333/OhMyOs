# W07 - 网卡中断诊断 + 性能测试矩阵收口

**周期**：2026-08-02 ~ 2026-08-08
**分支**：`net-k3`

## 本周做了什么

本周完成了两项工作：MMIO 网卡中断可诊断基线落地并归档，吸取异步串口经验，提前设计网卡性能测试矩阵。

## VirtIO-MMIO 可诊断中断基线

**为什么先做 IRQ 诊断**：异步 RX 需要 ISR → waker → queue task。如果 IRQ 投递没被证明可重复，排障时无法区分是 waker 漏了还是 IRQ 没到。先单独把中断路径跑通，再往上叠异步。

**做法**：QEMU UART 从全局 hook 迁到 IRQ 10 设备 handler。VirtIO-net 注册 IRQ 7 诊断 handler。诊断 handler 只读 status、分类 cause、写 ACK、更新 Relaxed atomics。不唤醒 queue task，不碰 descriptor。数据面保持 10ms 轮询兜底不变。

三个独立模块：

| 模块 | 文件 | 职责 |
|---|---|---|
| status decoder | `classify_mmio_status()` | 纯逻辑，将 32-bit 寄存器值解码为可用/已用/错误 cause |
| telemetry | `IrqTelemetry` | Relaxed atomic 计数器，记录各类中断发生次数 |
| host harness | 20 个测试 | 覆盖正常、错误、边界与组合 |

`VirtIoNetDev` 仍以 `irq=None` 构造，轮询数据面不退化。

**验证**：12/12 QEMU gates PASS：

- 启动签名、idle 无 spurious IRQ
- UART IRQ 10 独立于 net IRQ 7
- RX、TX、Both 各两次
- 轮询网络阶段 TCP/UDP 回归、协议栈本地化 14/14 回归
- guest C probe 5 modes 全部通过
- ioctl `0x4e494431` snapshot 正常

三个 runtime bug 修复：

1. `InterruptStatus`/`InterruptACK` 是 32-bit 寄存器（非 u8），按 u8 读写导致计数器全零
2. device_id=1 是网卡（非 2）
3. `axhal::irq::register` 接受 `fn()` handler，不是带参数的

commit [`2a9319a`](https://github.com/daivy2333/StarryOS/commit/2a9319a)

## 网卡性能测试矩阵收口

**为什么在异步 RX 前做**：异步 RX 引入后，性能变化需要对照基线才能判断是改善还是退化。先在轮询态固定 workload、指标、完成语义和记录格式，异步 RX 阶段再用同一套口径比较。

**做法**：基于已有轮询数据面，定义统一测试体系：

三层结构：

```text
统一 workload 协议
  -> 平台测量适配层（QEMU host、guest、真板）
  -> 统一 Evidence 与报告 Schema
```

测试目录 N00-N54 覆盖吞吐、延迟、丢包、burst、背压、CPU/IRQ/指令效率。每个测试固定 protocol、direction、payload、flow、profile。

三个拓扑不得合并统计：

| 拓扑 | 用途 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| guest loopback | 协议栈对照 | syscall、buffer、smoltcp 上层成本 | VirtIO 设备路径 |
| QEMU user-net | 兼容 smoke | hostfwd 下的功能 | 设备路径上限 |
| QEMU TAP | 正式性能基线 | guest 到 host 完整 VirtIO 路径 | 真板 DMA/cache/PHY |

六方向（TCP/UDP × RX/TX/BIDI）是基本执行单位。每个方向输出 manifest、round 和 reason-coded 结果。

三级结果判定：

| 层 | PASS | FAIL |
|---|---|---|
| 执行 | 双端启动并输出 manifest/round | hang、crash、无法建连 |
| 正确性 | fingerprint 一致，C6 账本闭合 | invalid、异常计数或账本不符 |
| 性能 | valid round、采样覆盖负载 | invalid round 或 capability 缺失 |

user-net 六方向已跑通执行资格。有 3 个 invalid（TCP RX partial、UDP RX buffer full、UDP TX late），2 个 valid（TCP TX、部分方向）。这些结果证明命令、协议记录和失败分类机制可运行。不代表性能结论。

**不变量**：

- `send()` 返回只代表 C1 enqueue，不是链路送达
- goodput 必须用 C6 receiver 校验字节
- RTT 只在同一时钟域测量
- 缺失 capability 写 `unavailable`，不写零
- QEMU 与真板分别建立独立基线
- A/B 只允许 `treatment` 字段不同

**覆盖状态**：

已执行：user-net 六方向执行资格、manifest 校验、工具自测、CPU collector 校准。

已有入口但未运行：TAP 六方向、2/4/8 flows、payload 阶梯、quick/standard profile、UDP pacing、idle CPU 对照。记为 `not-run`，不等于 PASS。

基础设施缺失：TCP RTT、UDP burst 精确数、背压恢复指标、guest inst/byte、IRQ/packet、timer interference、allocator/descriptor 内部遥测。记为 `infrastructure-unavailable`。新增支持需要独立的功能需求。

缺口分类规则见[资格扫描操作手册](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/network-benchmark-platform-qualification.md)。

两张关键文档：

- [基准测试分析文档](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/analysis/starryos-virtio-mmio-network-benchmark-baseline.md)（729 行）— 测试目录、指标公式、完成语义、记录格式
- [资格扫描操作手册](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/network-benchmark-platform-qualification.md)（588 行）— 操作流程、六方向命令模板、覆盖状态与缺口分类

commit [`2ccb836`](https://github.com/daivy2333/StarryOS/commit/2ccb836)

## 下周

两个任务。

**轮询网卡测试基线**：把测试代码写完，在 QEMU 上跑通轮询网卡的 TAP 六方向、多流和 payload 阶梯，产出轮询性能基线数据。这是异步改造之前必须拿到的对照基线。

**异步网卡开发启动**：新开分支，推进异步收包队列的需求分析、方案设计和任务分解。这是第一条异步路径的规划入口，预计持续数周。

## 参考

- [异步网卡架构探索](../notes/异步网卡/async-nic-architecture-exploration.md)
- [网卡性能测试矩阵研究](../notes/异步网卡/nic-benchmark-matrix-research.md)
- [异步网卡路线分析](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/analysis/starryos-async-network-roadmap.md)
- [基准测试分析文档](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/analysis/starryos-virtio-mmio-network-benchmark-baseline.md)
- [资格扫描操作手册](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/network-benchmark-platform-qualification.md)
- [中断诊断阶段操作记录](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/ms03-virtio-mmio-irq-evidence.md)
- [轮询网络阶段操作记录](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/ms02-virtio-mmio-evidence.md)

## 证据

| 阶段 | 证据 | 说明 |
|---|---|---|
| 中断诊断 | 12/12 QEMU gates PASS | IRQ 7 独立于 UART IRQ 10，无 spurious events |
| 中断诊断 | guest C probe 5 modes PASS | idle/tx/rx/both/repeat 全部通过 |
| 中断诊断 | host harness 20/20 PASS | status decoder + telemetry 纯逻辑测试 |
| 测试矩阵 | [user-net 六方向](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/network-benchmark-platform-qualification.md) | 证明命令、协议和失败分类可运行 |
| 测试矩阵 | [基准分析文档](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/analysis/starryos-virtio-mmio-network-benchmark-baseline.md) | manifest、checksum、collector 均通过 |
| 中断诊断 | [操作记录](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/ms03-virtio-mmio-irq-evidence.md) | 详细执行步骤与门禁结果 |

