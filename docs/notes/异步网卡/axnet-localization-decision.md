# smoltcp 接入与 axnet 本地化决策

**日期**：2026-08-01
**标签**：smoltcp, axnet, network, dependency, capability, polling-fallback, device-mask

## 背景

StarryOS 网络栈依赖 registry 上的 `axnet` crate（package name `axnet-ng`，v0.3.0-preview.2），axnet 又依赖 registry 的 `smoltcp`。

异步 NIC 主线启动时碰两条卡点：

1. registry axnet 调 smoltcp 的 `RxToken::preprocess`，smoltcp 0.13.1 移除了该方法，编译失败。
2. `Service::register_waker` 只走 IRQ 唤醒。轮询驱动 `irq=None` 没 IRQ 可注册，协议定时器到期无人唤醒，协议栈卡死。

两条都要改 axnet，registry 版本动不了。把 smoltcp 和 axnet 整套本地化到 `crates/` 下。

## 本地化范围

`crates/` 下五个本地化 crate：

| crate | 来源 | 用途 |
|---|---|---|
| `crates/smoltcp/` | smoltcp 0.13.1 | TCP/IP 协议栈 |
| `crates/axnet/` | axnet-ng 0.3.0-preview.2 | 网络服务（socket、router、service） |
| `crates/axfs-ng/` | axfs-ng | 文件系统（axnet 间接依赖） |
| `crates/axplat-riscv64-lichee-d1/` | 本地新增 | D1 平台适配 |
| `crates/uart_16550/` | uart_16550 | UART 驱动 |

[`Cargo.toml#L106`](https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/Cargo.toml#L106) 用 path 依赖指向本地 smoltcp：

```toml
[dependencies.smoltcp]
path = "../smoltcp"
version = "0.13.1"
features = [
    "alloc", "log", "async",
    "medium-ethernet", "medium-ip",
    "proto-ipv4", "proto-ipv6",
    "auto-icmp-echo-reply",
    "socket-raw", "socket-icmp", "socket-udp", "socket-tcp", "socket-dns",
]
default-features = false
```

`auto-icmp-echo-reply` 是无 IRQ 轮询阶段加的。smoltcp 默认要 guest 侧手动回 ICMP echo reply。开了这个 feature 后协议栈自动回。

## RxToken::preprocess 私有依赖问题

smoltcp 的 `Device` trait 用 token 模式：driver 调 `receive()` 拿到 `(RxToken, TxToken)` 对，RxToken 调 `consume()` 把包交给协议栈处理。旧版 smoltcp 的 `RxToken` trait 有一个 `preprocess` 方法，registry axnet 在调用 `receive` 后会先调 `preprocess` 做预检（比如判断是不是 ARP），再决定是否 `consume`。

smoltcp 0.13.1 移除了 `preprocess`。[当前 trait 定义](https://github.com/daivy2333/smoltcp/blob/f96a26b/src/phy/mod.rs#L381)：

```rust
// crates/smoltcp/src/phy/mod.rs:381
pub trait RxToken {
    fn consume<R, F>(self, f: F) -> R
    where
        F: FnOnce(&[u8]) -> R;

    fn meta(&self) -> PacketMeta {
        PacketMeta::default()
    }
}
```

只有 `consume` 和 `meta`。registry axnet 的 `receive` 路径还在调 `preprocess`，编译失败。

本地化的 axnet 改用 `poll_ingress_single` + `poll_egress`，不再走 `preprocess`：

```rust
// crates/axnet/src/service.rs:64 (poll)
pub fn poll(&mut self, sockets: &mut SocketSet) -> bool {
    let timestamp = now();
    let mut changed = false;

    self.router.poll(timestamp);
    self.iface.poll_maintenance(timestamp);
    LISTEN_TABLE.reconcile(sockets);
    loop {
        match self.iface.poll_ingress_single(timestamp, &mut self.router, sockets) {
            PollIngressSingleResult::None => break,
            PollIngressSingleResult::PacketProcessed => {}
            PollIngressSingleResult::SocketStateChanged => changed = true,
        }
        LISTEN_TABLE.reconcile(sockets);
    }
    loop {
        match self.iface.poll_egress(timestamp, &mut self.router, sockets) {
            PollResult::None => break,
            PollResult::SocketStateChanged => changed = true,
        }
    }
    LISTEN_TABLE.reconcile(sockets);
    self.router.dispatch(timestamp) || changed
}
```

`poll_ingress_single` 一次处理一个 ingress 包，循环到 `None` 为止。`poll_egress` 类似。这样 axnet 不依赖 `preprocess`，可以跟 smoltcp 0.13.1 编译。

## capability 边界：requires_polling 与 register_waker

[`Device` trait](https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/device/mod.rs#L15) 加了一个新方法：

```rust
// crates/axnet/src/device/mod.rs:15
pub trait Device: Send + Sync {
    fn name(&self) -> &str;
    fn recv(&mut self, buffer: &mut PacketBuffer<()>, timestamp: Instant) -> bool;
    fn send(&mut self, next_hop: IpAddress, packet: &[u8], timestamp: Instant) -> bool;

    /// Returns whether this device needs periodic polling to make progress.
    fn requires_polling(&self) -> bool {
        false
    }

    fn register_waker(&self, waker: &Waker);
}
```

`requires_polling` 默认 `false`（loopback 不需要轮询）。[`EthernetDevice`](https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/device/ethernet.rs#L336) 在 `irq_num().is_none()` 时返回 `true`：

```rust
// crates/axnet/src/device/ethernet.rs:336
fn requires_polling(&self) -> bool {
    self.inner.irq_num().is_none()
}

fn register_waker(&self, waker: &Waker) {
    if let Some(irq) = self.inner.irq_num() {
        register_irq_waker(irq, waker);
    }
}
```

`register_waker` 只在 `irq_num()` 返回 `Some` 时注册 IRQ waker。`None` 时啥也不做。

为什么 `None` 时不能不管？因为 smoltcp 的 `iface.poll_at()` 会返回协议定时器（比如 TCP 重传）的下次到期时间。`Service::register_waker` 拿这个时间挂 `sleep_until`。如果设备没 IRQ、`register_waker` 啥也不做，协议定时器到期时没人唤醒 `Service::poll`，协议栈卡死。

无 IRQ 轮询的做法：[`register_waker`](https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/service.rs#L110) 在协议定时器外挂 10ms 兜底：

```rust
// crates/axnet/src/service.rs:110
const POLLING_FALLBACK: Duration = Duration::from_millis(10);

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

两条 deadline 取 `min`：协议定时器更早就等协议，否则最多等 10ms。这样轮询驱动也能跑端到端，且不污染未来的 IRQ waker 路径。

## device mask 纯策略单测模式

`register_waker` 接收 `mask: u32`，bit `i` 表示第 `i` 个设备。判断"mask 命中的设备是否需轮询"的逻辑原本埋在 `register_waker` 里，测它要构造 `Service` + `Router` + `EthernetDevice` + 真设备，无法单测。

[抽成纯函数](https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/service.rs#L37)：

```rust
// crates/axnet/src/service.rs:37
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

入参是 `mask` 和一个 `bool` 迭代器，第 `i` 项是第 `i` 个设备的 `requires_polling()` 结果。遍历找"mask 选中且需轮询"的设备。

测试时传 `[true, false]` 数组即可：

```rust
#[test]
fn masked_polling_device_triggers_fallback() {
    let mask = 0b001;
    let capabilities = [true];
    assert!(any_masked_device_requires_polling(mask, capabilities));
}

#[test]
fn unmasked_polling_device_does_not_trigger_fallback() {
    let mask = 0b010;
    let capabilities = [true, false];
    assert!(!any_masked_device_requires_polling(mask, capabilities));
}
```

这种模式的好处：mask×eligibility 的所有组合（命中/未命中 × 轮询/不轮询）都能用一个 `[bool; N]` 数组覆盖，不依赖任何设备状态。8 个 test case 全过：4 个 `select_wake_deadline` 的 `min` 选择 + 4 个 `any_masked_device_requires_polling` 的 mask×eligibility 组合。

## 要点

本地化 smoltcp 和 axnet 的原因：上游 smoltcp 0.13.1 移除了 `RxToken::preprocess`，registry axnet 还在调。本地化后可以走新 API，代价是后续 smoltcp 升级要自己 rebase。

`requires_polling` 的设计把轮询和 IRQ 的差异从 `EthernetDevice` 内部提到 trait method 边界。`Service::register_waker` 只靠 `requires_polling()` 决定是否挂 10ms 兜底，不关心设备类型。后续异步 RX 上来后，`irq_num()` 改成 `Some(7)`，`requires_polling()` 自动变 `false`，兜底自动关。切换点收敛于 `Option<usize>`。

device mask 纯策略 helper 的测试模式——把状态判断抽成 `bool` 迭代器的纯函数，传数组即可覆盖 mask×eligibility 所有组合，不依赖设备。后续 waker 注册、queue 选择、budget 分配可复用。

## 参考

- [VirtIO 网卡队列机制入门](virtio-net-queue-intro.md)
- [`Device` trait 定义](https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/device/mod.rs#L15)
- [`EthernetDevice::requires_polling` / `register_waker`](https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/device/ethernet.rs#L336)
- [`Service::register_waker` / `any_masked_device_requires_polling`](https://github.com/daivy2333/StarryOS/blob/b35fcaf/crates/axnet/src/service.rs#L110)
- [`RxToken` trait（smoltcp 子模块）](https://github.com/daivy2333/smoltcp/blob/f96a26b/src/phy/mod.rs#L381)
