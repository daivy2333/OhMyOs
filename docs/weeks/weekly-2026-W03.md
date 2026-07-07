# W03 - Q19B userbench 完成 + Q19C-M0 benchmark evidence cleanup

**周期**：2026-07-05 ~ 2026-07-13

> 分支：`uart-16550-lichee`（领先 origin 10 commits）
>
> 提交数：11 个（5 个 DW APB UART / axfs-ng / loader + 4 个 repo + 2 个 docs）
>
> 代码定位：所有 `path:L{N}` 引用均指向 `uart-16550-lichee` 分支当前 HEAD `217fdd7`；commit 链接用短 SHA 7 位

## 上下文

阶段一+二（Q16-Q19 smoke）已在真板输出 `[starry-d1] smoke complete, halting.`。本文记录：阶段二剩余方案延伸、阶段三（Q19B）完整交付、阶段四（Q19C-M0）benchmark evidence cleanup。

阶段三目标：在真板跑通 embedded `benchmark.elf`，验证 `/dev/console`、TTY、syscall、`tcdrain`、FIONBIO 全链路。

阶段四目标：统一 QEMU/D1 benchmark manifest，消除测量污染。诊断 D1 TX zero-send/P99 长尾，实施 slow-pool + yield 重试 fallback。

详见 [`weekly-2026-W02`](weekly-2026-W02.md) 的 Q19 smoke 收尾状态。

## 阶段二延伸：硬件能力 + 运行模式拆分

阶段二结尾发现用户态运行时（`/dev/console`、syscall、task-ext）被 `lichee-d1-smoke` / `lichee-d1-kbench` 的模块排除规则误伤。Q19B 在阶段二基础上重新做 feature 拆分（ADR-050）：

| 类别 | feature | 作用 |
|------|---------|------|
| 平台 smoke | `lichee-d1` | smoke 回归（保持向后兼容）|
| 硬件能力 | `lichee-d1-async-uart` | DW APB UART stride 4 + 真 PLIC |
| 运行模式 | `lichee-d1-kbench` | kernel benchmark 后 halt |
| 运行模式 | `lichee-d1-userbench` | 含 axfs/pseudofs/syscall 的最小用户态 runtime |

```toml
# kernel/Cargo.toml:14-18
lichee-d1 = []
lichee-d1-smoke = []
lichee-d1-async-uart = []                    # 硬件能力（DW APB UART + PLIC）
lichee-d1-kbench = ["lichee-d1-async-uart"]  # 运行模式：kernel benchmark 后 halt
lichee-d1-userbench = ["lichee-d1-async-uart", "dep:axfs", "axfeat/paging", "axfeat/task-ext"]
```

kbench 的模块排除不影响 userbench。

## 阶段三实施（Q19B 2026-06-29）

1. **DW APB UART 32-bit MMIO UartPort**（ADR-048）：新文件 `kernel/src/drivers/d1_uart.rs`（162 行）实现 `ArceOsD1UartPort`。内部用 stride-aware `read_reg(offset)` / `write_reg(offset, val)` 封装 `base_ptr.add(offset * stride).cast::<u32>().read_volatile()`。复用 `AsyncUartDriver` 类型系统与 `uart_16550::async_::isr` 的 `RX_WAKER` / `TX_WAKER` / `DRAIN_WAKER`（[`c820567`](https://github.com/daivy2333/StarryOS/commit/c820567)）。
2. **axfs-ng 本地 patch**（ADR-049）：`crates/axfs-ng/Cargo.toml` 改 `axdriver` 依赖，强制 `block + bus-mmio`（[`e7d1933`](https://github.com/daivy2333/StarryOS/commit/e7d1933)）。
3. **embedded benchmark ELF loader**：在 `kernel/src/mm/loader.rs:357` 新增 `load_embedded_user_app()`。通过 `include_bytes!` 嵌入 `kernel/resources/benchmark.elf`，绕过文件系统（`解析 ELF → 分配用户映射 → uspace.write()`）（[`4ba2f75`](https://github.com/daivy2333/StarryOS/commit/4ba2f75)）。

### 踩坑 6：feature 继承导致 userbench 编译失败

**症状**：`cargo check --target riscv64gc-unknown-none-elf --features lichee-d1-userbench` 报缺 `crate::drivers::ASYNC_TTY`、`crate::file`、`crate::mm`、`crate::pseudofs`、`crate::task`、`axfs`、`axtask::AxTaskExt`。

**根因**：`kernel/src/lib.rs` 和 `kernel/src/drivers/mod.rs` 用 `#[cfg(not(any(feature = "lichee-d1-smoke", feature = "lichee-d1-kbench")))]` 排除用户态路径模块；同时旧版本 `lichee-d1-userbench = ["lichee-d1-kbench"]` 让 userbench 继承会排除文件/任务/伪文件系统的 kbench-only feature。

**修复**：拆分硬件能力与运行模式（ADR-050），见上节 feature 表（[`fcb008d`](https://github.com/daivy2333/StarryOS/commit/fcb008d)）。

### 踩坑 7：Cargo.lock 版本污染

**症状**：对 workspace 内 path dependency 执行 `cargo check --manifest-path` 后 `Cargo.lock` 中 `axcpu 0.3` 被升级到 `0.3.1`，破坏版本对齐。

**根因**：`axcpu` 在 `Cargo.toml` 中用 `axcpu = "0.3"` 松约束，被新解析自动升级。

**修复**：根 `Cargo.toml:122` 用 `[patch.crates-io] axcpu = { ... }` 本地锁定，并在 `crates/axplat-riscv64-lichee-d1/Cargo.toml` 显式 `axcpu = "=0.3.0-preview.8"` 精确锁死。误升级后 `git restore Cargo.lock` 恢复（[`c820567`](https://github.com/daivy2333/StarryOS/commit/c820567)）。

### 踩坑 8：axdriver build.rs 输出的 cfg 绕过 axfeat 弱转发

**症状**：启用 `axfeat/bus-mmio` 后仍编译 `axdriver/src/bus/pci.rs`，链接报 `PCI_ECAM_BASE` 未定义。

**根因**：`axdriver` 的 `build.rs` 根据自身是否启用 `bus-mmio` feature 输出自定义 cfg：有 `bus-mmio` → `cargo:rustc-cfg=bus="mmio"`；否则默认 `cargo:rustc-cfg=bus="pci"`。`axfeat/bus-mmio = ["axdriver?/bus-mmio"]` 只转发给 `axfeat` 自己的可选依赖 `axdriver`，不会影响 `axfs-ng` 间接拉进来的 `axdriver`。

**修复**：在本地 patch 的 `crates/axfs-ng/Cargo.toml` 显式写：

```toml
# crates/axfs-ng/Cargo.toml:18
axdriver = { version = "=0.3.0-preview.2", default-features = false, features = ["block", "bus-mmio"] }
```

（[`e7d1933`](https://github.com/daivy2333/StarryOS/commit/e7d1933)）。

### 踩坑 9：embedded ELF 含 relocation

**症状**：`load_embedded_user_app()` 解析 `kernel/resources/benchmark.elf` 后在用户空间跳转到 `0x0` 触发 page fault。

**根因**：`riscv64-linux-musl-gcc -static` 产出 `DYN` / static PIE，带 `R_RISCV_RELATIVE` relocation；当前 loader 不做 relocation。

**修复**：`gcc -static -no-pie -fno-pie -s`；`file` + `readelf -h` + `readelf -r` 验证 `Executable file` 与 `There are no relocations in this file.`。`load_embedded_user_app` 见 [`kernel/src/mm/loader.rs:357`](https://github.com/daivy2333/StarryOS/blob/217fdd7/kernel/src/mm/loader.rs#L357)：

```rust
#[cfg(feature = "lichee-d1-userbench")]
pub fn load_embedded_user_app(
    uspace: &mut AddrSpace,
    elf_data: &[u8],
    args: &[String],
    envs: &[String],
) -> AxResult<(VirtAddr, VirtAddr)> {
    // ET_EXEC（base=0）路径；ET_DYN/PIE 路径不处理 relocation
    let elf_parser =
        ELFParser::new(&elf, crate::config::USER_SPACE_BASE).map_err(|_| AxError::InvalidData)?;
```

（[`4ba2f75`](https://github.com/daivy2333/StarryOS/commit/4ba2f75)）。

### 踩坑 10：THRE 边沿丢失导致 TX 不唤醒

**症状**：真板日志显示 PLIC 进入 UART IRQ 18，但 IIR 多次为 `0xc1`（bit0=1 no pending），有效 THRE `0xc2` 只偶发；userbench 64B write 后 tcdrain 不返回。

**根因**：QEMU 会稳定触发 THRE 中断，掩盖了"启用 THRE 时硬件已 ready 但不再产生新边沿"的真板窗口。D1 `ArceOsD1UartPort::update_ier(THR_EMPTY)` 在 LSR 已 THRE/TEMT 时未立即软件 wake。

**修复**（ADR-051）：D1 backend 启用 THRE 时立即软件 wake；ISR 在 IIR bit0=1 时基于 LSR 补 wake：

```rust
// kernel/src/drivers/d1_uart.rs:178
fn update_ier(&self, set: IER, clear: IER) {
    let mut val = self.ier_cache.load(Ordering::Relaxed);
    val |= set.bits();
    val &= !clear.bits();
    self.ier_cache.store(val, Ordering::Relaxed);
    self.write_reg(UART_IER, val as u32);

    if set.contains(IER::THR_EMPTY) {
        use uart_16550::async_::isr;
        let lsr = self.read_reg(UART_LSR);
        if lsr & LSR_THRE != 0 { isr::TX_WAKER.wake(); }
        if lsr & LSR_TEMT != 0 { isr::DRAIN_WAKER.wake(); }
    }
}
```

```rust
// kernel/src/drivers/d1_uart.rs:198
fn d1_uart_isr_handler() {
    let lsr = read_reg(UART_LSR);
    if lsr & LSR_THRE != 0 { isr::TX_WAKER.wake(); }
    if lsr & LSR_TEMT != 0 { isr::DRAIN_WAKER.wake(); }
    if lsr & LSR_DR != 0 { isr::RX_WAKER.wake(); }
    // ... IIR dispatch above
}
```

（[`d4a8b63`](https://github.com/daivy2333/StarryOS/commit/d4a8b63)）

### 踩坑 11：tcdrain 漏覆盖 staged/TEMT

**症状**：Q19B 首次跑 userbench，64B write 后 `tcdrain` 永久挂起；增大 wait timeout 后最终返回但耗时异常。

**根因**：`flush()`/`sys_ioctl(TCSBRK)` 只注册 TX ring waker，未覆盖 TX copier 已 pop 到 staged buffer 后的 `staged_bytes -> 0` 与 UART TEMT 变化。

**修复**：`flush()` 始终注册 `DRAIN_WAKER`；TX copier 在最后一批送完且 TEMT 后主动 wake drain：

```rust
// crates/uart_16550/src/async_/driver.rs:489
self.tx_staged_bytes
    .fetch_sub(sent, Ordering::AcqRel);
if last_batch {
    // TEMT corner-case: 主动 wake drain 等待者
    DRAIN_WAKER.wake();
}
```

四阶段 drain 快照：

```rust
// crates/uart_16550/src/async_/driver.rs:288
pub struct TxCompletion {
    pub ring_empty: bool,
    pub copier_active: bool,
    pub staged_bytes: usize,
    pub transmitter_empty: bool,
}

impl TxCompletion {
    pub fn is_drained(&self) -> bool {
        self.ring_empty && !self.copier_active
            && self.staged_bytes == 0 && self.transmitter_empty
    }
}
```
> 这部分后续会回写到专门的 UART 仓库。

（[`b1d15e3`](https://github.com/daivy2333/StarryOS/commit/b1d15e3)）

### Q19B 阶段初步数据（2026-06-29，已被 Q19C.8e 覆盖）

Q19B 首次跑通 embedded userbench 时的初步数据。最新指标见阶段四（Q19C.8e）。

镜像产物：

| Make 目标 | 输出 | kernel_size |
|-----------|------|-------------|
| `make lichee` | `starry-lichee-boot.img` | 118976 bytes |
| `make lichee-kbench` | `starry-lichee-kbench-boot.img` | 188608 bytes |
| `make lichee-userbench` | `starry-lichee-userbench-boot.img` | 876736 bytes（Q19B）/ 970944 bytes（Q19C.8e）|

## 阶段四：Q19C-M0 benchmark evidence cleanup（2026-07-07）

Q19B 完成后，Q19C-M0 统一 QEMU/D1 benchmark manifest，诊断 D1 TX copier 的 `hw_send_zero` / `no_progress_budget_exhausted` / P99 长尾（[`7a13a46`](https://github.com/daivy2333/StarryOS/commit/7a13a46)、[`217fdd7`](https://github.com/daivy2333/StarryOS/commit/217fdd7)）。

### 横向对比（QEMU rootfs vs D1 userbench）

`q19c-m0-20260703` 同版用户态 benchmark 在 QEMU 与 D1 真板输出同构指标。QEMU 不仿真物理线延迟（`line_rate_pct > 100%` 为预期），D1 大包稳定接近 11.52 KB/s 理论线速，作绝对线速依据。

| 指标 | QEMU rootfs | Lichee D1 userbench | 解释 |
|------|-------------|---------------------|------|
| TX baseline 64B drain-each | 153.86 KB/s | 11.13 KB/s | D1 达 96.6% 线速（pre-section drain 后消除 backlog 污染）|
| TX baseline 256B drain-each | 167.20 KB/s | 11.21 KB/s | D1 达 97.3% 线速 |
| TX baseline 1024B drain-each | 182.28 KB/s | 11.38 KB/s | D1 达 98.8% 线速 |
| TX batch-drain 64B | 159.34 KB/s | 11.38 KB/s | D1 64B batch-drain 达 98.8% 线速 |
| TX writev 4x64B | 149.88 KB/s | 11.36 KB/s | 两端均出现 1 字节短写 |
| TX 1B P99 | 0.357 ms | 0.224 ms | D1 1B 不触发 FIFO full |
| RX empty nonblocking | PASS | PASS | `open(O_NONBLOCK)` 与 `ioctl(FIONBIO)` 均返回 EAGAIN |

### 内核态 startup benchmark

衡量 async UART 驱动内部 ring buffer 与统计路径，不代表硬件 UART 线速。

| 指标 | QEMU rootfs | Lichee D1 userbench | 测量条件 |
|------|-------------|---------------------|----------|
| Ring buffer TX push | 550,055 KB/s | 1,151,569 KB/s | 102400 bytes，100 x 1024B |
| RX ring buffer read | 1,205,273 KB/s | 8,437,706 KB/s | 65536 bytes |
| RX latency P99 | 11,600 ns | 246 ns | n=100，单字节 ring pop |
| Driver struct | 152 bytes | 272 bytes | Q19C.8e 加 slow-pool/yield 计数器 |
| IRQ count at report | 0 | 43 | startup benchmark 阶段 |

D1 driver struct 从 152 bytes 增加到 272 bytes（Q19C.8e slow-pool + yield 计数器）。D1 IRQ count 43 反映 slow-pool 期间 TX_WAKER 注册后 ISR 更频繁到达。

### benchmark.c 统一

- S00 manifest 输出 benchmark version、target mode、startup chain、root provider、TX 矩阵、RX 模式
- 移除 4096B 默认测试（缩短真板运行时间）
- 移除 S10 drain-each-recheck 和 S11 second-drain（已证明无信息量）
- S11 加 gated TX debug snapshot：`hw_send_zero` / `no_progress_budget_exhausted` / `hw_send_max_chunk` / `slow_poll_exh` / `yield_exh`

### 64B 测量污染消除

旧数据 D1 64B `write+tcdrain` 约 1 KB/s（8.8% 线速）。根因是进入 S10 前 stdout backlog 在 D1 上 `pre_section_stdout_drain_ms=5688`，把 manifest/日志排空时间计入首个小包段。每节开始加 `fflush(stdout); tcdrain(STDOUT_FILENO)` 并打印 pre-drain。隔离后 D1 64B 达 10.7-11.1 KB/s（93-97% 线速）。

### D1 TX copier 修复（Q19C.8d）

1. `ArceOsD1UartPort::send_bytes()` 在 THRE 后一次填最多 16B FIFO（`hw_send_max_chunk` 从 1 提到 16，S11 1024B 正确发送恢复）。
2. TTY OPOST/ONLCR short-write 计数修复（S11 1024B `short_writes` 从高数字降到 36）。

### Q19C.8e slow-pool + yield 重试

TX copier 在 budget exhausted（32 次 fast retry 失败）后加入 bounded slow-pool（`TX_SLOW_POLL_LIMIT=4096` × `TX_SLOW_POLL_SPINS=256`），给 FIFO 排空时间后 retry `send_bytes`。slow-pool 失败后再 yield 重试（`TX_YIELD_RETRIES=4` 自唤醒），最后才 fallback 到纯 ISR 等待。

`TX_FAST_RETRY_LIMIT=0` + drain 注册 `TX_WAKER` 的方案已证伪——benchmark 进程启动后卡住，D1 THRE 唤醒路径不能作为唯一进展来源。

### Q19C.8e 真板数据（2026-07-07）

三轮数据对比（S11 final-drain txdbg）：

| 轮次 | size=64 hw_send_calls | hw_send_zero | no_progress_budget | slow_poll_exh | yield_exh | P99 (256B) |
|------|----------------------|--------------|--------------------|---------------|-----------|------------|
| 基线 | 13,966 | 13,566 | 399 | - | - | 50.872ms |
| +slow-pool | 274,218 | 273,818 | 399 | - | - | 50.868ms |
| +yield | 274,396 | 273,996 | 399 | 0 | 0 | 50.860ms |

`slow_poll_exh=0` 证明 slow-pool 100% 成功（每次 budget exhausted 后约 653 次 send_bytes 后 FIFO 排空）。`yield_exh=0` 证明 yield 重试从未触发。P99 长尾根因未探明——slow-pool/yield 均未改善，`slow_over_line_plus10ms=1`（100 次中 1 次超出线时+10ms）。当前影响可接受（吞吐量 <2%），暂不继续优化。

D1 真板最终性能（Q19C.8e 后）：

| 指标 | 值 |
|------|----|
| 64B TX drain-each | 11.13 KB/s（96.6% 线速）|
| 256B TX drain-each | 11.21 KB/s（97.3% 线速）|
| 1024B TX drain-each | 11.38 KB/s（98.8% 线速）|
| 64B TX batch-drain | 11.38 KB/s（98.8% 线速）|
| 1B tcdrain P99 | 0.224 ms |
| FIONBIO 双入口 | PASS |
| benchmark exit code | 0 |

镜像产物（Q19C.8e 后）：

| Make 目标 | 输出 | kernel_size |
|-----------|------|-------------|
| `make lichee-userbench` | `starry-lichee-userbench-boot.img` | 983232 bytes |

### Q19C.8e 产出位置

| 路径 | 用途 | 关键 commit |
|------|------|-------------|
| `crates/uart_16550/src/async_/driver.rs` | `TX_SLOW_POLL_LIMIT`/`TX_SLOW_POLL_SPINS`/`TX_YIELD_RETRIES` 常量 + slow-pool + yield 重试逻辑 + `slow_poll_exhausted`/`yield_retries_exhausted` 计数器 | [`7a13a46`](https://github.com/daivy2333/StarryOS/commit/7a13a46)、[`217fdd7`](https://github.com/daivy2333/StarryOS/commit/217fdd7) |
| `tests/benchmark.c` | S00 manifest + S11 gated TX debug snapshot + 移除 S10 recheck/S11 second-drain | [`7a13a46`](https://github.com/daivy2333/StarryOS/commit/7a13a46)、[`217fdd7`](https://github.com/daivy2333/StarryOS/commit/217fdd7) |
| `kernel/src/syscall/fs/ctl.rs` | `UartTxDebugSnapshot` 加 `slow_poll_exhausted`/`yield_retries_exhausted` 字段 | [`217fdd7`](https://github.com/daivy2333/StarryOS/commit/217fdd7) |

## 阶段三产出位置

| 路径 | 用途 | 关键 commit |
|------|------|-------------|
| [`kernel/src/drivers/d1_uart.rs`](https://github.com/daivy2333/StarryOS/blob/217fdd7/kernel/src/drivers/d1_uart.rs) | `ArceOsD1UartPort`（stride 4 / 32-bit MMIO）| [`c820567`](https://github.com/daivy2333/StarryOS/commit/c820567) |
| [`kernel/src/drivers/d1_uart.rs:178`](https://github.com/daivy2333/StarryOS/blob/217fdd7/kernel/src/drivers/d1_uart.rs#L178) | `update_ier` 软件 wake | [`d4a8b63`](https://github.com/daivy2333/StarryOS/commit/d4a8b63) |
| [`kernel/src/drivers/uart_init.rs`](https://github.com/daivy2333/StarryOS/blob/217fdd7/kernel/src/drivers/uart_init.rs) | QEMU/D1 双路径 feature gate | [`c820567`](https://github.com/daivy2333/StarryOS/commit/c820567) |
| [`crates/axfs-ng/Cargo.toml:18`](https://github.com/daivy2333/StarryOS/blob/217fdd7/crates/axfs-ng/Cargo.toml#L18) | `axdriver` 强制 `block + bus-mmio` | [`e7d1933`](https://github.com/daivy2333/StarryOS/commit/e7d1933) |
| [`kernel/src/mm/loader.rs:357`](https://github.com/daivy2333/StarryOS/blob/217fdd7/kernel/src/mm/loader.rs#L357) | `load_embedded_user_app()` | [`4ba2f75`](https://github.com/daivy2333/StarryOS/commit/4ba2f75) |
| [`kernel/src/entry.rs`](https://github.com/daivy2333/StarryOS/blob/217fdd7/kernel/src/entry.rs) | `lichee_d1_init()` 三模式分发 | [`c820567`](https://github.com/daivy2333/StarryOS/commit/c820567) |
| [`kernel/Cargo.toml:14-18`](https://github.com/daivy2333/StarryOS/blob/217fdd7/kernel/Cargo.toml#L14) | feature 拆分 | [`fcb008d`](https://github.com/daivy2333/StarryOS/commit/fcb008d) |
| [`kernel/resources/benchmark.elf`](https://github.com/daivy2333/StarryOS/blob/217fdd7/kernel/resources/benchmark.elf) | 嵌入式用户态 benchmark | [`4ba2f75`](https://github.com/daivy2333/StarryOS/commit/4ba2f75) |

## ADR 索引（阶段三新增）

| ADR | 标题 | 状态 |
|-----|------|------|
| ADR-047 | Q19B 先嵌入 benchmark payload，再追求 SDMMC/rootfs parity | ✅ 已接受 |
| ADR-048 | D1 先做平台专用 UartPort，后考虑 width-aware backend | ✅ 已落地 |
| ADR-049 | Q19B Phase 5-6 通过最小 axfs-ng patch 解阻 | ✅ 已落地 |
| ADR-050 | Q19B feature 必须区分硬件能力与运行模式 | ✅ 已落地 |
| ADR-051 | D1 async UART drain 必须兼容 THRE 边沿丢失 | ✅ 已落地 |
| ADR-052 | Q19C fullbench 范围收敛：M0 evidence cleanup + M1 path loader + M3 SDMMC probe-only | ✅ 已落地 |

## learned 条目索引（阶段三）

[`openspec/specs/learned/spec.md`](https://github.com/daivy2333/StarryOS/blob/217fdd7/openspec/specs/learned/spec.md)：

- L236-L239：Q19B benchmark 依赖链 + async UART 首要阻塞 + PLIC 代码已存在但未启用 + embedded ELF 推荐
- L240-L244：Q19B feature 拆分方案 + DW APB UART 实现 + uart_init 双路径 + axfs 阻塞解决 + cargo check 三模式
- L245-L251：feature 继承陷阱 + 五阻塞全景 + 五步路线 + feature 规范化实战 + 最小依赖集 + embedded ELF 加载 + 最终 host gate
- L252-L258：axdriver cfg(bus) 不是普通 feature + axfeat 弱转发不能修复间接 axdriver + embedded ELF 必须禁用 PIE + THRE 边沿丢失 + tcdrain 必须覆盖 staged/TEMT + 真板性能基线 + CRLF/exit drain
- L259-L266：Q19C 路径差异 + fullbench feature 边界 + rootfs provider 分层 + benchmark 证据口径 + 64B 测量污染 + TX drain/THRE 长尾排查
- L275：Q19C.8e slow-pool + yield 重试真板验证结果（slow_poll_exh=0 证明 slow-pool 100% 成功，P99 根因未探明）

## 当前状态

- Q19/Q19B 已完成真板验证并归档。
- Q17 QEMU 修复完成（`ier_cache` RMW 临界区 + TX completion 原子序），多 hart stress 待 Q20。
- Q19C-M0 已完成（benchmark evidence cleanup + slow-pool + yield 重试，P99 长尾根因未探明，暂不继续优化）。
- Q19C M1 memory-root path loader 是下一步（`/bin/benchmark` 通过 `FS_CONTEXT.resolve()` + `load_user_app()`）。
- Q19D 真实 D1 SDMMC/rootfs 已登记为后续方向。
- Q20 VisionFive2 UART 验证等待硬件。

## 参考

- 前序：见 [`weekly-2026-W02`](weekly-2026-W02.md)
- `docs/licheerv-dock-bringup.md`：流程笔记（2026-06-28 起持续更新）
- [`openspec/changes/q19-lichee-d1-early-smoke/`](https://github.com/daivy2333/StarryOS/tree/217fdd7/openspec/changes/q19-lichee-d1-early-smoke)：Q19 变更提案
- [`openspec/changes/q19b-lichee-d1-benchmark/`](https://github.com/daivy2333/StarryOS/tree/217fdd7/openspec/changes/q19b-lichee-d1-benchmark)：Q19B 变更提案
- `.claude/analysis/q19b-current-blockers.md`：Q19B 阻塞分析
- `.claude/analysis/q19b-lichee-benchmark-plan.md`：Q19B benchmark 方案
- `openspec/specs/learned/spec.md` L213-L258：踩坑与经验
- `openspec/specs/architecture/spec.md` A040-A051：相关 ADR