# W02 - Lichee RV Dock 真板 smoke 完成（Q16-Q19）

**周期**：2026-06-27 ~ 2026-07-04

> 分支：`uart-16550-lichee`（领先 origin 10 commits）
> 提交数：13 个（5 个 docs + 8 个 D1 axplat + smoke 适配）
> 代码定位：所有 `path:L{N}` 引用均指向 `uart-16550-lichee` 分支当前 HEAD `3b82cd3`；commit 链接用短 SHA 7 位

## 工作流

Q15 在 QEMU 完成增量融合与 Manual QA 验证后，单一 Q6 真板桶被拆分为按 Gate 类型分层的 Q16~Q23。原计划将 VisionFive2 作为第一块真板，Lichee RV Dock 因手头可用被插入作为流程演练板。

Lichee 的价值是提前打通烧录、串口、启动链与基础 PTE 修复。Q17 SMP 内存序验证不在 Lichee 范围内（D1 是单核 C906）。

## 阶段一：Q16-Q18 准备（2026-06-27 至 2026-06-28）

**目标**：roadmap 重排 + 平台参数解耦 + early console 抽象。

- Q16：原 Q6 拆分为 Q16~Q23；traffic-light 触发条件分层。commit [`e2f060e`](https://github.com/daivy2333/StarryOS/blob/e2f060e/docs/uart-async/)。
- Q17：完成 O63 实施前分析，确认 `ier_cache` RMW 竞争是真板 4 核下 `Blocking` 阻塞点。commit [`e9af446`](https://github.com/daivy2333/StarryOS/blob/e9af446/docs/uart-async/)。
- Q18：新增 platform descriptor（[`kernel/src/platform/descriptor.rs:17`](https://github.com/daivy2333/StarryOS/blob/941ad05/kernel/src/platform/descriptor.rs#L17) `ConsoleUart { kind, base, irq, stride, width }`），把 QEMU UART facts 从 `uart_init.rs` 抽出；新增不依赖 IRQ/async/rootfs 的 polling early console（[`kernel/src/platform/early_console.rs:19`](https://github.com/daivy2333/StarryOS/blob/941ad05/kernel/src/platform/early_console.rs#L19) `trait EarlyConsole`）。commit [`941ad05`](https://github.com/daivy2333/StarryOS/blob/941ad05/)。

### 踩坑 1：QEMU UART 常量散落在驱动初始化路径

**症状**：早期 Lichee 适配曾尝试只改 `uart_init.rs` 中 base 为 `0x02500000`，但 DW APB UART 要求 stride 4 + 32-bit MMIO，纯改地址不能复用现有 NS16550 backend。

**根因**：`kernel/src/drivers/uart_init.rs` 硬编码 QEMU UART `base=0x10000000` / stride 1 / `iomap(...,0x1000)`。`make MYPLAT=...` 仍然访问 QEMU 地址。

**修复**：`kernel/src/platform/descriptor.rs` + `kernel/src/platform/{qemu,lichee_d1,visionfive2}.rs` 集中所有平台事实。Driver init 路径只消费 descriptor：

```rust
// kernel/src/platform/descriptor.rs:17
pub struct ConsoleUart {
    pub kind: &'static str,
    pub base: usize,
    pub irq: usize,
    pub stride: usize,
    pub width: usize,  // 1 = U8 (NS16550), 4 = U32 (DW APB UART)
}
```

commit [`941ad05`](https://github.com/daivy2333/StarryOS/blob/941ad05/)。

**效果**：`make ARCH=riscv64 build` QEMU 行为不变；驱动初始化路径不再新增板级常量。

## 阶段二：Q19 D1 smoke（2026-06-28 至 2026-06-29）

**目标**：在 Lichee RV Dock 真板输出 `[starry-d1] smoke complete, halting.`。

**实施**：

- 新增 `crates/axplat-riscv64-lichee-d1/`（11 文件：boot/console/init/irq/mem/power/time/lib.rs + axconfig.toml + build.rs + Cargo.toml）。
- Cargo/Make 接入：`dep:axplat-riscv64-lichee-d1` feature + `MYPLAT`/`PLAT_CONFIG` + `DWARF=n` + 移除原 `cp` hack。Makefile 入口 [`Makefile:61`](https://github.com/daivy2333/StarryOS/blob/afafb31/Makefile#L61)：

  ```makefile
  $(MAKE) ARCH=riscv64 APP_FEATURES=lichee-d1 MYPLAT=axplat-riscv64-lichee-d1 \
      PLAT_CONFIG=$(PWD)/crates/axplat-riscv64-lichee-d1/axconfig.toml \
      MEM=512M BUS=mmio DWARF=n build
  ```

- D1 link/load contract：ELF entry `0xffffffc040200000`、boot image `kernel_addr=0x40200000`。
- D1 axplat UART0 32-bit MMIO polling console（DW APB UART，stride 4）。

### 踩坑 2：链接阶段 `IrqIf` 符号未定义

**症状**：`make lichee` 链接报 `undefined reference to '__IrqIf_register' / '__IrqIf_set_enable' / '__IrqIf_handle'`。

**根因**：D1 build 触发 `axplat/irq` 接口符号，但完整 PLIC 不能提前启用（PLIC 上下文初始化依赖中断状态）。

**修复**：`irq-if` feature 启用 `axplat/irq`，搭配 no-op `IrqIfImpl` 满足运行时符号但不启用 PLIC：

```rust
// crates/axplat-riscv64-lichee-d1/src/irq_stub.rs:9
struct IrqIfImpl;

impl IrqIf for IrqIfImpl {
    fn set_enable(_irq: usize, _enabled: bool) {}
    fn register(_irq: usize, _handler: IrqHandler) -> bool { true }
    fn unregister(_irq: usize) -> Option<IrqHandler> { None }
    fn handle(_irq: usize) -> Option<usize> { None }
}
```

完整 PLIC 实现保留在 `irq.rs`（feature `irq`），Q19B 启用。commit [`4a228be`](https://github.com/daivy2333/StarryOS/blob/4a228be/crates/axplat-riscv64-lichee-d1/src/irq_stub.rs)。

### 踩坑 3：板测 `Store/AMO access fault`

**症状**：U-Boot 已加载 `d1-nezha` boot image，但 `Starting kernel ...` 后报 `Store/AMO access fault EPC ffffffc040244648 TVAL ffffffc0402c6908`。

**根因**：符号化 EPC 位于 `percpu::imp::init` 的 `amoor.w.aqrl`，TVAL 是 `.bss` 中 `percpu::imp::IS_INIT`。D1/C906 early DDR page table 缺 T-Head C9xx normal-memory 属性 bits。

**修复**：early boot DDR identity/high-half mapping 设 `PTE_DDR = 0xef | (1<<60) | (1<<61) | (1<<62)`；`lichee-d1` feature 启用 `page_table_entry/xuantie-c9xx`：

```rust
// crates/axplat-riscv64-lichee-d1/src/boot.rs:16
const THEAD_NORMAL_MEMORY: u64 = (1 << 60) | (1 << 61) | (1 << 62);
const PTE_DDR: u64 = PTE_VRWX_GAD | THEAD_NORMAL_MEMORY;

BOOT_PT_SV39[1] = (0x40000 << 10) | PTE_DDR;       // DDR identity
BOOT_PT_SV39[0x101] = (0x40000 << 10) | PTE_DDR;   // DDR high-half mirror
```

commit [`4a228be`](https://github.com/daivy2333/StarryOS/blob/4a228be/crates/axplat-riscv64-lichee-d1/src/boot.rs)。后续 final page table 也带相同属性（[`kernel/src/platform/lichee_d1.rs`](https://github.com/daivy2333/StarryOS/blob/3b82cd3/kernel/src/platform/lichee_d1.rs)）。

### 踩坑 4：boot image 超过分区尺寸

**症状**：未传 `DWARF=n` 时 raw binary 达 `25.6M`，超过 boot 分区约 `10.1M`，`fastboot flash` 报空间不足。

**根因**：`rust-objcopy --strip-debug` 对当前链接布局无效（调试段仍进 raw binary）。

**修复**：`Makefile:61` 强制 `DWARF=n`。最终 `kernel_size=118976` bytes，落在分区容量内。

### 踩坑 5：virtio + PCI 常量缺失导致 smoke 路径崩溃

**症状**：`starry-lichee-boot.img` 烧录后卡在 `axdriver` 初始化，触发 `No block device found!` 与 `PCI_ECAM_BASE`/`PCI_RANGES`/`PCI_BUS_END` undefined。

**根因**：D1 无 virtio-mmio、无 PCI。axconfig 中 `virtio-mmio-ranges = [[0,0]]` 会让 `axdriver_virtio::probe_mmio_device` 访问 `phys_to_virt(0)`，fault VA 表现为 `0xffffffc000000000`。未隔离 fs/net/display/axdriver/PCI/task-ext 时会触发 PCI 总线初始化。

**修复**：

```toml
# crates/axplat-riscv64-lichee-d1/axconfig.toml:25
virtio-mmio-ranges = [] # [(uint, uint)]
```

Lichee smoke 路径在 `kernel/src/lib.rs` 用 `#[cfg(not(feature = "lichee-d1"))]` 排除 net socket、fb/axdisplay、virtio/display 模块；syscall 中的 socket 分派函数逐项加 `#[cfg(not(feature = "lichee-d1"))]`。commit [`afafb31`](https://github.com/daivy2333/StarryOS/blob/afafb31/crates/axplat-riscv64-lichee-d1/axconfig.toml)。

### Q19 收尾事实

2026-06-29 真板串口输出：

```
platform = riscv64-lichee-d1
sbi_version: 0.2
[starry-d1] early boot
[starry-d1] smoke complete, halting.
```

Q19a 收尾。下一步进入 Q19B（embedded userbench）。

## 阶段一+二产出位置

| 路径 | 用途 | 关键 commit |
|------|------|-------------|
| [`crates/axplat-riscv64-lichee-d1/`](https://github.com/daivy2333/StarryOS/tree/3b82cd3/crates/axplat-riscv64-lichee-d1) | D1 axplat crate | [`4a228be`](https://github.com/daivy2333/StarryOS/blob/4a228be/crates/axplat-riscv64-lichee-d1/) |
| `crates/axplat-riscv64-lichee-d1/src/boot.rs:16` | T-Head C9xx PTE bits | [`4a228be`](https://github.com/daivy2333/StarryOS/blob/4a228be/crates/axplat-riscv64-lichee-d1/src/boot.rs#L16) |
| `crates/axplat-riscv64-lichee-d1/src/irq_stub.rs:9` | no-op `IrqIf` | [`4a228be`](https://github.com/daivy2333/StarryOS/blob/4a228be/crates/axplat-riscv64-lichee-d1/src/irq_stub.rs#L9) |
| `crates/axplat-riscv64-lichee-d1/axconfig.toml:25` | `virtio-mmio-ranges = []` | [`afafb31`](https://github.com/daivy2333/StarryOS/blob/afafb31/crates/axplat-riscv64-lichee-d1/axconfig.toml#L25) |
| [`kernel/src/platform/descriptor.rs:17`](https://github.com/daivy2333/StarryOS/blob/3b82cd3/kernel/src/platform/descriptor.rs#L17) | platform descriptor | [`941ad05`](https://github.com/daivy2333/StarryOS/blob/941ad05/kernel/src/platform/descriptor.rs#L17) |
| [`kernel/src/platform/early_console.rs:19`](https://github.com/daivy2333/StarryOS/blob/3b82cd3/kernel/src/platform/early_console.rs#L19) | `trait EarlyConsole` | [`941ad05`](https://github.com/daivy2333/StarryOS/blob/941ad05/kernel/src/platform/early_console.rs#L19) |
| [`Makefile:61`](https://github.com/daivy2333/StarryOS/blob/3b82cd3/Makefile#L61) | `make lichee` 入口 | [`afafb31`](https://github.com/daivy2333/StarryOS/blob/afafb31/Makefile#L61) |

## ADR 索引（阶段一+二新增）

| ADR | 标题 | 状态 |
|-----|------|------|
| [ADR-040](https://github.com/daivy2333/StarryOS/blob/3b82cd3/openspec/specs/architecture/spec.md) | Q20 真板启动 PLIC / Clock "trust u-boot" 模式（UART 不含） | 🟡 Proposed |
| ADR-041 | Q18 真板 PLIC init_primary/init_percpu 防御性设计 | 🟡 防御性保留 |
| ADR-042 | Q17 SMP 原子内存序按语义选择 | ✅ 已接受 |
| ADR-043 | Lichee Android boot image + D1 polling early console 分阶段 | ✅ 已接受 |
| ADR-044 | 多平台 platform descriptor 集中化 | ✅ 已接受 |
| ADR-045 | D1 真板正路径必须接入 D1 axplat | ✅ 已接受 |
| ADR-046 | D1/C906 early page table 必须设 T-Head normal-memory PTE | ✅ 已接受 |

## learned 条目索引（阶段一+二）

[openspec/specs/learned/spec.md](https://github.com/daivy2333/StarryOS/blob/3b82cd3/openspec/specs/learned/spec.md)：

- L213-L216：Lichee 板级事实（UART/boot/路线）
- L217-L220：平台参数解耦 + early console 分层
- L221-L222：D1 当前无输出根因 + axplat 构建 gate
- L223-L227：axplat 版本对齐 + axconfig_macros 类型标注 + `BUS=mmio` 必要 + Cargo.lock 污染修复 + IrqIf 分层
- L228-L235：D1 boot image 尺寸 + AMO fault 根因 + final page table 风险 + smoke 完成事实 + virtio 空 MMIO + feature gate

## 当前状态（阶段二收尾）

- Q16/Q17/Q18/Q19 已完成。
- Q17 SMP 内存序修复仍待做（O63）。
- Q19B Lichee D1 async UART userbench 阶段待启动。
- Q20 VisionFive2 UART 验证等待硬件。

## 参考

- `docs/licheerv-dock-bringup.md`：流程笔记（2026-06-28 起持续更新）
- [`openspec/changes/q19-lichee-d1-early-smoke/`](https://github.com/daivy2333/StarryOS/tree/3b82cd3/openspec/changes/q19-lichee-d1-early-smoke)：Q19 变更提案
- `.claude/analysis/lichee-rv-dock-adaptation-plan.md`：适配方案
- `.claude/analysis/platform-parameter-decoupling.md`：平台解耦分析
- `.claude/analysis/d1-axplat-bringup-plan.md`：D1 axplat 方案
