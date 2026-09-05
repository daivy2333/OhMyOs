# 异步网卡的手动 QEMU 测试：MS1 到 MS7 在测什么

**日期**：2026-09-05
**标签**：rust, async, network, nic, qemu, test, acceptance, driver

> 异步网卡的每一阶段收口，都靠两类验证：单元 / 主机侧自动测试，加上在 QEMU 里手工跑的验收。本文说明后者——MS1 到 MS7 那套手动测试各自测什么、为什么不能省、合起来证明什么。

## 为什么需要手动测试

设备驱动的很多行为无法用主机上的单元测试覆盖：中断触发与 ack 的时序、DMA 与队列描述符的真实转发、并发唤醒的竞争，以及设备出错或链路断开时的恢复路径，这些只能在真实运行环境里看。QEMU 提供的是接近真板但可控的环境——能注入故障、能开关链路，代价是不涉及真板 DMA/cache/IRQ/SMP 的细节。

## 手工验收的通用套路

从 MS4 起，每套手动验收都有相同的骨架，只是场景不同：

- **Guest 探针**（`tests/ms0x_*.c` 编译成的 RISC-V payload）：在 guest 里跑，逐条发布 `PASS` marker。
- **Host 侧伴生**：HTTP server 把 payload 从 host 下到 guest（`wget http://10.0.2.2:18765/...`）；UDP stimulus/peer 在 host 上与 guest 对打（常见的 15556 / 15557 / 15572 端口）。
- **QEMU**：单 hart、单 VirtIO-MMIO NIC、user-net，全程用 `script` 录制串口。
- **Validator**：跑完后用纯输出脚本对原始串口离线审计，按 marker 判 PASS/FAIL，不做人工摘录。

## 从 MS1 到 MS7：每套测什么

| 阶段 | 手动测试内容 | 验证的核心主张 |
|---|---|---|
| MS1 | socket 基线（14 项） | 协议栈本地化后，基础 socket 行为没坏 |
| MS2 | 轮询网络（TCP/UDP 5555、ARP/ICMP、空闲 CPU） | VirtIO-MMIO 同步轮询能端到端收发 |
| MS3 | 中断诊断（guest probe：idle/uart/rx/tx/both/repeat） | IRQ 正确注册、分类、ack，且独立于 UART |
| MS4 | 异步收包（snapshot/idle/nudge/burst） | 收包靠中断唤醒、空闲不忙轮询、有界不丢包 |
| MS5 | 有界双向数据面（snapshot/tx-only/bidirectional/slot-full/descriptor-full/flush） | 收发共用固定槽位，满则恢复，冲刷闭合 |
| MS6 | 应用可见异步网络（12-case readiness） | socket 就绪桥到 select/poll，多等待者正确唤醒 |
| MS7 | 故障恢复（6 case + HMP link flap） | 设备 reset、分层取消、超时、链路断开/恢复都安全 |

### MS1 — socket 基线（14 项）

最低的回退底线。协议栈本地化（把 smoltcp/axnet 挪进仓库）之后，先证明原来的 TCP/UDP socket 还能正常工作，再往上叠异步。之后每个阶段收口都重跑它，防止新改动悄悄破坏基础收发。对应 `tests/ms01_socket_baseline.c`。

### MS2 — VirtIO-MMIO 轮询基线

当时还没上中断，驱动靠定时查看（轮询）推进。手动验证集中在：user-net 上走 5555 端口的 TCP/UDP 端到端、TAP 上的 ARP/ICMP、以及空闲时 CPU 是否安静。它证明 MMIO 这条传输路径确实通了，为后续中断化准备一块干净的底。

### MS3 — 可诊断中断基线

把中断先钉清楚再谈异步。guest probe 在 idle / 仅 UART / RX / TX / 双中断 / 重复 RX 几种输入下跑，确认 NET IRQ 7 和 UART IRQ 10 两个 handler 都注册、能分类、能正确 ack/rearm、且互不干扰。另配纯逻辑 host harness 覆盖可单测的控制面。这步决定了后面"中断只唤醒、不搬数据"的 ISR 边界。

### MS4 — 异步收包核心（snapshot/idle/nudge/burst）

第一个真正的异步阶段，用四种模式分别测：

- **snapshot**：内部状态一致可读（owner、lifecycle、fault 计数）。
- **idle**：静置时 IRQ、软件事件、任务、descriptor、budget delta 全为 0——证明空闲真的在睡觉，没有空转轮询。
- **nudge**：一次软件唤醒只让任务醒来一次且不搬数据——证明唤醒是事件驱动，不是每步都动。
- **burst**：连收 96 个包，reaped/refilled/delivered 三个 delta 都等于 96、消耗过 budget、发生过自让位、无 fault——证明有界吞吐与 descriptor 回收守恒。

### MS5 — 有界双向数据面（六模式）

把收和发放进同一套固定槽位规则，测六种模式：

- **snapshot**：状态快照一致性。
- **tx-only / bidirectional**：只发 / 双向收发指定数量（96 包）。
- **slot-full / descriptor-full**：发到槽满或描述符满时进入恢复、出来能继续——背压与恢复路径。
- **flush**：一批数据真正全部发完才返回（ticketed flush），不是"塞进队列就算成功"。

### MS6 — 应用可见异步网络（12-case readiness）

测协议栈自己独立推进、socket 就绪状态桥到 select/poll 之后，应用能否被正确唤醒。12 个 readiness case 覆盖多等待者、关闭、错误、轮询后实际 I/O 一致性等场景；跑完在同一会话里重跑 MS1/4/5 作回归。

### MS7 — 故障恢复（6 case + HMP link flap）

最后一个阶段，测"正常路径断了怎么收尾"：

- **pre_reset_traffic**：恢复前流量正常。
- **reset_request**：触发整设备复位，设备走 Active→Quiescing→Resetting→Reinitializing→Active。
- **old_socket_terminal**：复位前创建的旧 socket 在恢复后稳定返回终止错误，不复活。
- **new_epoch_traffic**：新时代的新 socket 能重新双向收发。
- **hmp_link_down / hmp_link_up**：用 QEMU monitor `set_link net0 off/on` 模拟链路断开/恢复——断开不伪造完成、恢复不复活旧连接。

整段靠串口录制的原始日志由 validator 离线判定。

## 它们合起来证明什么

MS1→MS7 是一条逐层加厚的主线：**轮询能用 → 中断正确 → 异步收包 → 双向有界数据面 → 应用可见 → 故障恢复**。每一层都改动内核里"怎么知道数据好了、谁来搬、满了怎么办"的那段结构，所以每一层收口都对此前全部套件做累积回归——MS7 收口时一次性重跑过 MS1/4/5/6。

## 局限

这套证据只覆盖 **single-hart QEMU 的 VirtIO-MMIO 软件/设备模型**。它不证明 SMP、PCI/DWMAC、真板上的 DMA cache/coherency/IRQ 行为，也不含性能结论。真板行为由另外的上板流程验证。

## 参考

- [异步网卡手动测试逐项：每个 PASS 在验什么](async-nic-manual-tests-per-item.md)：MS1 到 MS7 每一项断言明细
- [网卡异步化：全链路异步已打通](nic-async-status-device-done-app-pending.md)：各阶段状态与机制
- [网卡收发的异步化：为什么做、怎么做](async-nic-rx-tx-why-how.md)：设备侧异步化的动机与方法
- [异步网卡架构探索](async-nic-architecture-exploration.md)：整体分层与方案取舍