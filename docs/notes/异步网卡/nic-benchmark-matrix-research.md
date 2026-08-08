# 网卡性能测试矩阵研究

**日期**：2026-08-08
**标签**：rust, network, benchmark, testing, virtio, qemu, performance, matrix

> 来源：StarryOS 基准测试分析文档和资格扫描操作手册。
> 本阶段位于中断诊断完成之后、异步 RX 开发之前，目的是在引入异步前固定测试口径和基线方法。

## 为什么做测试矩阵

异步 RX 引入后，性能变化需要对照基线才能判断改善或退化。如果不在轮询态先把 workload、指标公式、完成语义和 Evidence 格式定下来，后面每次改驱动都要重新讨论"怎么测"。

本阶段不产生性能 B0 结论。它固定复用的测试口径。将来 QEMU、真板、轮询、异步都用同一套测试 ID、公式和记录格式。换平台或换驱动只替换适配层，测试语义不变。

## 测试目录

测试分为五组，N00-N54。每个测试固定 protocol、direction、payload、flow、profile 六个维度。

| 组 | 编号 | 内容 | 等级 |
|---|---|---|---|
| 校准 | N00-N03 | manifest、时钟校准、loopback 对照、路径校准 | 必测 |
| 吞吐 | N10-N14 | TCP 单向、写尺寸、双向、多流、稳态 | 必测 |
| 延迟 | N20-N24 | TCP RTT、UDP 吞吐/RTT/burst、负载下延迟 | 必测 |
| 边界 | N30-N35 | 背压、队列边界、连接周转、缓冲边界、复制效率 | 机制 |
| 效率 | N40-N46 | 空闲成本、CPU 效率、IRQ 效率、调度干扰、内存、唤醒、descriptor | 必测/机制 |
| 扩展 | N50-N54 | 网络损伤、长稳、过载恢复、SMP 多队列、真板机制 | 扩展/真板 |

测试支持四个 profile：

| Profile | 用途 | 时长 | warm-up | 当前执行 |
|---|---|---|---|---|
| smoke | 功能、握手、短校准 | 1 s | 0 | ✅ |
| quick | 开发回归 | 5 s | 1 s | ✅ |
| standard | 正式 B0/A1 比较 | 10 s | 2 s | ✅（Schema 固定，未生成 B0） |
| soak/board | 长稳、损伤、SMP、真板 | 300 s+ | 按需 | ❌ |

## 三层完成语义

网络发送有六个完成点，但测试只用三个关键层：

```text
C1 syscall 返回     ← send() 返回，只代表数据进入 socket buffer
C4 descriptor 回收  ← 设备释放 descriptor，buffer 可回收
C6 peer 校验       ← 接收端完成校验并回复摘要
```

- `send()` 返回（C1）：enqueue 指标。不能表示 peer 已收到。
- descriptor 回收（C4）：驱动物理完成。需要内部遥测。
- peer 校验（C6）：goodput 真值。吞吐和正确性指标以此为准。

吞吐指标全部使用 C6 receiver 校验字节。RTT 只在同一时钟域内测量（一端发、一端回），不报告 one-way latency。

## 测试拓扑

三个拓扑不得合并统计：

| 拓扑 | 用途 | 能证明什么 |
|---|---|---|
| guest loopback | 协议栈对照 | syscall、buffer、smoltcp 上层成本 |
| QEMU user-net | 兼容 smoke | NAT、hostfwd 下的功能可用性 |
| QEMU TAP | 正式性能基线 | guest 到 host 完整 VirtIO 路径 |

user-net 在 QEMU 进程内增加用户态网络栈。入站依赖 hostfwd，ICMP 受限。只做兼容 smoke，不产出性能结论。

TAP 将 guest NIC 接到主机虚拟接口。主机可运行原生 peer、抓包和流量整形。吞吐、RTT、丢包、CPU 基线固定使用 TAP。

QEMU 与真板分别建立独立基线。比较轮询到异步的相对变化，不比较绝对吞吐或 RTT。

## 六方向执行模型

TCP/UDP × RX/TX/BIDI 六个方向是基本执行单元。每个方向输出：

- manifest（版本、配置 hash、capability bitmap）
- round（原始记录，含 invalid reason）
- 双端账本（sent、accepted、received、errors）

运行流程：

```text
1. 双端交换版本与参数 → capability 协商
2. receiver 就绪
3. warm-up（不计入结果）
4. 测量屏障
5. data transfer
6. receiver 完成校验
7. 返回接收字节、包、错误和时间
8. 双端输出固定格式记录
```

无效 round 原样保留。补跑使用新 round ID。不能删除 outlier 或静默重跑。

## 三级结果判定

| 层 | PASS 条件 | FAIL 条件 |
|---|---|---|
| 执行 | 双端启动，输出 manifest 和 round | hang、crash、无法建连 |
| 正确性 | fingerprint 一致，C6 账本闭合 | invalid reason、异常计数、账本不符 |
| 性能 | round valid，采样覆盖负载，capability 可用 | invalid round 或 capability 缺失 |

invalid round 可以通过执行资格。只有 valid round 才能进入性能统计。

## 当前覆盖状态

当前在 QEMU user-net 环境（轮询驱动）取得的资格：

| 场景 | 执行 | 正确性 | 说明 |
|---|---|---|---|
| TCP RX | PASS | invalid | host 9964 TX；guest 7788 RX（partial） |
| UDP RX | PASS | invalid | host 60822 TX；guest 27 late（buffer full） |
| TCP TX | PASS | valid | 双端 4702 packets、6582800 B |
| UDP TX | PASS | invalid | guest 4819 TX；host 4812 RX、7 late |
| TCP BIDI | PASS | invalid | 单方向闭合，反向未闭合 |
| UDP BIDI | PASS | invalid | 双向流量存在，账本未闭合 |

这些结果证明六方向命令、协议记录和失败分类可运行。不代表网卡性能结论。

## 基础设施已支持但未执行

这些项目 CLI 入口已就绪，当前未取得运行记录（TAP 环境待执行）：

| 项目 | 已有入口 |
|---|---|
| TAP TCP/UDP 六方向 | server/client、TAP 拓扑、pcap |
| 2/4/8 flows | `--flows` 参数 |
| TCP payload 1-2012 B | `--payload` 参数 |
| UDP payload 1-1436 B | `--payload` 参数 |
| quick/standard 时长 | `--profile`、`--duration`、`--warmup` |
| idle CPU 对照 | host collector、两组对照 |
| TAP pcap | tcpdump |

记为 `not-run`。不等于 PASS 或网卡功能正常。

## 基础设施缺失

这些测试口径 CLI、采集器或 telemetry 无法表达。记为 `infrastructure-unavailable`：

| 项目 | 缺失能力 |
|---|---|
| TCP RTT（N20） | 无 RTT request/reply 模式和原始样本收集 |
| UDP RTT/间隔误差（N22） | 无匹配 reply 和发送计划 |
| UDP exact burst（N23） | 无精确 datagram count 参数 |
| 负载下延迟（N24） | 无并行 RTT 流；offered load 非 pilot-relative |
| 背压恢复（N30） | 有 EAGAIN 处理，无 fill-to-EAGAIN 模式 |
| 队列边界（N31/N34） | 无 packet count、socket buffer 容量控制 |
| guest inst/byte（N41） | calibration 可读，workload round 未集成 delta |
| benchmark IRQ/packet（N42） | 中断诊断 probe 可用，benchmark round 未集成 snapshot |
| timer interference（N43） | 无 wake overshoot 原始样本 |
| allocator/descriptor 遥测（N44-N46） | 无内部计数设施 |

`infrastructure-unavailable` 不等于网卡失败。新增支持需要独立的功能需求，不属于重复执行命令。

两类缺口的区分规则：命令能表达但没跑 → `not-run`；CLI/采集器/遥测根本不支持 → `infrastructure-unavailable`。不能混记。

## 工具链

| 文件 | 作用 |
|---|---|
| [`tests/network_benchmark.c`](https://github.com/daivy2333/StarryOS/blob/net-k3/tests/network_benchmark.c) | guest/host 共用 workload |
| [`tests/network_benchmark_protocol.h`](https://github.com/daivy2333/StarryOS/blob/net-k3/tests/network_benchmark_protocol.h) | 控制协议和记录格式 |
| [`tests/network_benchmark_platform.h`](https://github.com/daivy2333/StarryOS/blob/net-k3/tests/network_benchmark_platform.h) | 时钟、计数器和平台适配 |
| [`scripts/network_benchmark_collect.py`](https://github.com/daivy2333/StarryOS/blob/net-k3/scripts/network_benchmark_collect.py) | host CPU/RSS 采样 |
| [`scripts/network_benchmark_report.py`](https://github.com/daivy2333/StarryOS/blob/net-k3/scripts/network_benchmark_report.py) | 原始记录 → CSV/JSON 摘要 |
| [`scripts/network_benchmark_evidence.py`](https://github.com/daivy2333/StarryOS/blob/net-k3/scripts/network_benchmark_evidence.py) | 记录完整性与比较资格检查 |

guest 用 RISC-V musl 静态编译，host 用本机编译。同一 C 程序支持 client/server、TCP/UDP、TX/RX、RTT 和校验模式。

## Evidence 要求

每次正式运行保存：

```text
manifest.json          # 完整配置、capability、hash
qemu-command.txt       # 展开后的 QEMU 命令
qemu-serial.log        # 完整串口日志
guest-netbench.ndjson  # guest 原始记录
host-netbench.ndjson   # host 原始记录
host-cpu.ndjson        # QEMU/peer/collector CPU/RSS
irq-snapshots.log      # 中断诊断前后快照
capture.pcap           # TAP 数据包见证
results.csv            # 逐 round 规范化数据
summary.json           # 汇总与有效性状态
evidence-check.json    # 完整性与比较资格
```

原始 NDJSON 不可丢弃。CSV 和 JSON 摘要不能替代原始记录。

## 参考

- [网卡基准分析文档](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/analysis/starryos-virtio-mmio-network-benchmark-baseline.md)（729 行，完整测试目录与公式）
- [资格扫描操作手册](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/network-benchmark-platform-qualification.md)（588 行，操作流程与缺口分类）
- [QEMU 网络测试手册](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/qemu-network-testing.md)
- [轮询网络阶段操作记录](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/ms02-virtio-mmio-evidence.md)
- [中断诊断阶段操作记录](https://github.com/daivy2333/StarryOS/blob/net-k3/.claude/runbooks/ms03-virtio-mmio-irq-evidence.md)
- [基准分轴规则](https://github.com/daivy2333/StarryOS/blob/net-k3/openspec/specs/knowledge/spec.md)
- [基础设施缺口需求](https://github.com/daivy2333/StarryOS/blob/net-k3/openspec/specs/improvements/spec.md)

