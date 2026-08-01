# W06 - 网卡主线：本地化编译 + 无 IRQ 轮询 + IRQ 诊断启动

**周期**：2026-07-26 ~ 2026-08-01
**分支**：`net-k3`（从 `uart-lichee` 分出），6 个提交

## 本周做了什么

正在进行异步网卡开发的各种探索工作。

比如，smoltcp/axnet 本地化编译修复，VirtIO-MMIO 无 IRQ 轮询跑通端到端网络。

中间两个非预期问题：设备 `irq_num()` 返回 `None` 时反而要保留 10ms 轮询兜底；空闲 CPU 100-111% 是预期而非 busy loop。

## smoltcp/axnet 本地化与编译修复

**问题**：原 axnet 通过 `RxToken::preprocess` 调用 smoltcp 的私有 API。smoltcp 0.13.1 移除了这个方法，registry axnet 编译失败。后续异步改造也会被这条私有依赖卡住。

**做法**：把 smoltcp 0.13.1 和 axnet 整套本地化到 `crates/` 下，删掉 `RxToken::preprocess` 调用，改用 `poll_ingress_single` + `poll_egress` 推进 ingress/egress。TCP listener 加 sidecar 容量 512，egress 推到 `None` 才停。

**验证**：14/14 QEMU 手测 PASS（TCP listen/accept、UDP、nonblocking、poll 与同步行为一致）。

commit [`efcf081`](https://github.com/daivy2333/StarryOS/commit/efcf081)

## VirtIO-MMIO 无 IRQ 轮询

**问题**：本地化之后网卡尚无运行时数据面。axdriver 的 MMIO probe 传 `irq=None`，`EthernetDevice::irq_num()` 返回 `None`。如果暴露 IRQ 7 为 `AxNetDevice::irq_num()`，axnet 会关轮询兜底，走全局 net waker hook。那条路径 QEMU UART 当前占着，混进去异步排障和 IRQ 排障一起做，定位乱。

**做法**：axnet 加 `Device::requires_polling()` trait method（默认 `false`）。`EthernetDevice` 在 `irq_num().is_none()` 时返回 `true`。`Service::register_waker` 拿到 `true` 后挂 10ms `sleep_until` 兜底，和 smoltcp 的 `poll_at` 取 `min` 作为下次唤醒。轮询驱动跑端到端，不污染 IRQ 路径。

```rust
// crates/axnet/src/device/ethernet.rs#L336
// https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/device/ethernet.rs#L336
fn requires_polling(&self) -> bool {
    self.inner.irq_num().is_none()
}
```

```rust
// crates/axnet/src/service.rs#L110
// https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/service.rs#L110
pub fn register_waker(&mut self, mask: u32, waker: &Waker) {
    let timestamp = now();
    let protocol_deadline = self.iface.poll_at(timestamp, &SOCKET_SET.inner.lock());
    let polling_deadline = any_masked_device_requires_polling(
        mask,
        self.router.devices.iter().map(|d| d.requires_polling()),
    )
    .then_some(timestamp + POLLING_FALLBACK);
    let next = select_wake_deadline(protocol_deadline, polling_deadline);
    // ...
}
```

**为什么抽成纯函数**：mask×capability 的判断埋在 `register_waker` 里无法单测。抽成 `any_masked_device_requires_polling(mask, impl IntoIterator<Item=bool>)` 后，传 `[true, false]` 数组即可覆盖所有组合，不需要真设备。

```rust
// crates/axnet/src/service.rs#L37
// https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/service.rs#L37
fn any_masked_device_requires_polling(
    mask: u32,
    polling_capabilities: impl IntoIterator<Item = bool>,
) -> bool {
    polling_capabilities
        .into_iter()
        .enumerate()
        .any(|(i, requires_polling)| mask & (1 << i) != 0 && requires_polling)
}
```

8/8 unit PASS（4 个 deadline `min` 选择 + 4 个 mask×eligibility 组合）。

**QEMU 端到端取证**：三种网络路径分别取证，避免串口成功混进网络成功：

| 路径 | 见证 |
|---|---|
| 无 hostfwd 启动 | shell 可进，net/block 可探 |
| user-net | TCP/UDP 5555 各通 |
| TAP | ARP/ICMP 抓包可见（[pcap](https://github.com/daivy2333/StarryOS/blob/599ec78/openspec/changes/archive/2026-07-29-ms02-virtio-mmio-polling-baseline/evidence/003-policy-coverage-and-runtime-evidence/qemu-tap.pcap)） |

smoltcp 启用 `auto-icmp-echo-reply`，省掉 guest 侧手动回 ICMP。

**空闲 CPU 100-111% 的判定**：QEMU 单核 30 秒采样 100-111%（[原始数据](https://github.com/daivy2333/StarryOS/blob/599ec78/openspec/changes/archive/2026-07-29-ms02-virtio-mmio-polling-baseline/evidence/003-policy-coverage-and-runtime-evidence/idle-cpu.txt)）。`register_waker` 的 10ms `sleep_until` 是预期行为。smoltcp 无协议定时器时，polling fallback 每 10ms 唤醒做 `iface.poll`。单核无其他 hart 分担，timer 中断 + 调度 + poll 跑满。不是 busy loop——busy loop 不让出 CPU 给 `sleep_until`。

四轮验证，采集流程整理为[操作记录](https://github.com/daivy2333/StarryOS/blob/05dfcfc/.claude/runbooks/ms02-virtio-mmio-evidence.md)。

| commit | 内容 |
|---|---|
| [`d35afeb`](https://github.com/daivy2333/StarryOS/commit/d35afeb) | `requires_polling` + 10ms fallback + 纯策略 helper + guest payload |
| [`599ec78`](https://github.com/daivy2333/StarryOS/commit/599ec78) | 归档（4 轮验证，含 pcap） |
| [`ee8555b`](https://github.com/daivy2333/StarryOS/commit/ee8555b) | 功能规格同步到主文档 |
| [`05dfcfc`](https://github.com/daivy2333/StarryOS/commit/05dfcfc) | 操作记录 |

## IRQ 诊断启动

**为什么先做 IRQ 诊断再做异步 RX**：异步 RX 要把 ISR -> waker -> queue task 串起来。如果 IRQ 投递本身没被证明可重复，异步排障时无法区分"是 waker 漏了"还是"IRQ 没到"。先单独把 IRQ claim/cause/ack/EOI/rearm 跑通，再上层叠异步路径。

**做法**：QEMU UART 从全局 hook 迁到 IRQ 10 设备 handler。VirtIO-net 注册 IRQ 7 诊断 handler。诊断 handler 只读 status、分类 cause、写 ACK、更新 Relaxed atomics。不唤醒 queue task，不碰 descriptor。`VirtIoNetDev` 仍以 `irq=None` 构造，轮询数据面不变。

方案、设计、任务拆解已写完并审批通过。待实施。

分析文档 [`.claude/analysis/starryos-device-specific-irq-waker-architecture.md`](https://github.com/daivy2333/StarryOS/blob/c7df9fb/.claude/analysis/starryos-device-specific-irq-waker-architecture.md)（370 行）记录了设备专属 IRQ + waker 架构的设计依据。

commit [`c7df9fb`](https://github.com/daivy2333/StarryOS/commit/c7df9fb)

## 下周

然后当前k3开发板还没有回音，异步串口暂时停下，继续研究异步网卡的研究，另外我会另外创建分支用来测试当前网卡性能避免以后再回来做重复性的工作，也就是下一两周还要做点写测试以及分析的工作。

## 参考

- [VirtIO 网卡队列机制入门](../notes/virtio-net-queue-intro.md)
- [smoltcp 接入与 axnet 本地化决策](../notes/axnet-localization-decision.md)
- [MMIO：用 load/store 指令操作硬件](../notes/mmio-intro.md)
- [异步 UART 驱动总体架构](../notes/async-uart-driver-architecture.md)
- [IRQ + waker 架构分析](https://github.com/daivy2333/StarryOS/blob/c7df9fb/.claude/analysis/starryos-device-specific-irq-waker-architecture.md)
- [轮询网络验证采集](https://github.com/daivy2333/StarryOS/blob/05dfcfc/.claude/runbooks/ms02-virtio-mmio-evidence.md)

## 证据

| 阶段 | 证据 | 说明 |
|---|---|---|
| 本地化编译 | [14/14 QEMU 手测](https://github.com/daivy2333/StarryOS/blob/efcf081/openspec/changes/archive/2026-07-29-t01-smoltcp-axnet-baseline/evidence/002-bind-fmt-closeout/qemu-bind-witness.log) | TCP listen/accept、UDP、nonblocking、bind witness 全 PASS |
| 无 IRQ 轮询 | [TAP ARP/ICMP 抓包](https://github.com/daivy2333/StarryOS/blob/599ec78/openspec/changes/archive/2026-07-29-ms02-virtio-mmio-polling-baseline/evidence/003-policy-coverage-and-runtime-evidence/qemu-tap.pcap) | VirtIO-MMIO 端到端网络，ARP 请求/应答可见 |
| 无 IRQ 轮询 | [user-net TCP/UDP 抓包](https://github.com/daivy2333/StarryOS/blob/599ec78/openspec/changes/archive/2026-07-29-ms02-virtio-mmio-polling-baseline/evidence/003-policy-coverage-and-runtime-evidence/qemu-usernet.pcap) | TCP 5555、UDP 5555 收发正常 |
| 无 IRQ 轮询 | [空闲 CPU 采样](https://github.com/daivy2333/StarryOS/blob/599ec78/openspec/changes/archive/2026-07-29-ms02-virtio-mmio-polling-baseline/evidence/003-policy-coverage-and-runtime-evidence/idle-cpu.txt) | 30 秒采样，100-111% 占用 |
| 无 IRQ 轮询 | [8/8 策略单测](https://github.com/daivy2333/StarryOS/blob/599ec78/openspec/changes/archive/2026-07-29-ms02-virtio-mmio-polling-baseline/evidence/003-policy-coverage-and-runtime-evidence/policy-tests.log) | deadline min 选择 ×4 + mask×eligibility ×4 |
