# Lichee RV Dock Smoke 前问题清单

> 范围：StarryOS 在 Lichee RV Dock 冒烟测试完成前遇到的主要问题、症状与根因边界。
> 日期：2026-06-30
> 关联：`docs/lichee-adaptation-prework.md`、`docs/lichee-smoke-solutions.md`、`docs/licheerv-dock-bringup.md`。

## 问题分层

Smoke 前的问题按根因分五层：

- 镜像尺寸
- 启动协议
- 平台链接
- 页表属性
- 错误设备假设

每一层失败都会表现为"板子没跑起来"，但排查方法不同。把这些问题混在一起，会导致不断重烧镜像却没有定位进展。

表 1 按层级列出主要症状。它是排障时的分类索引，不是解决方案表。

| 层级 | 典型症状 | 初步判断 |
|---|---|---|
| 镜像尺寸 | `dd` 写 boot 分区报 `No space left on device` | 产物超过 boot 分区 |
| 启动协议 | U-Boot `Starting kernel ...` 后无 StarryOS 输出 | boot image 或 axplat 入口错误 |
| 平台链接 | 链接缺 `__IrqIf_*` 等符号 | D1 axplat interface 不完整 |
| 页表属性 | `Store/AMO access fault` | C906 memory attribute 缺失 |
| 设备假设 | `No block device found!`、PCI 常量缺失 | QEMU fs/block/PCI 假设泄漏到 D1 |

Smoke 前的主要目标：让最小 payload 在 D1 上稳定打印并停机。任何超出 early boot 的功能应先关掉。

## 镜像尺寸与 boot 分区限制

第一次替换 boot 时出现 `No space left on device`。根因是 StarryOS boot image 未关闭 DWARF，产物达 `25.6M`；官方 boot 分区备份约 `10.1M`。问题与内核代码正确性无关，属于镜像产物约束。

当前 Makefile 的 `lichee`、`lichee-kbench`、`lichee-userbench` 都显式传入 `DWARF=n`，证据见 `Makefile:60-82`。smoke 镜像最终 `kernel_size = 118976` bytes，远低于 boot 分区；kbench/userbench 也保持在可写范围内。

真板适配必须先建立产物尺寸 gate。否则 U-Boot、内核入口、页表是否正确都没有机会被验证。

## 启动协议与平台入口不匹配

Lichee 官方 U-Boot 期望 Android boot image，并使用 name `d1-nezha`、kernel load `0x40200000`、page size `2048`。若 StarryOS 按 QEMU axplat 或普通 ELF/裸 bin 思路组织启动，U-Boot 可显示 `Starting kernel ...`，但 StarryOS 不会进入正确的平台入口。

D1 平台事实后集中到 `kernel/src/platform/lichee_d1.rs:19-47`，其中 `BootKind::AndroidImage`、`load_paddr = 0x40200000`、UART0 `0x02500000` 是 smoke 成功的前提。`Makefile:60-66` 负责把 smoke kernel 打包为 Android boot image。

启动协议问题的特征：U-Boot 看似正常，但 OS 没有可信输出。此时应先检查 boot image header 和 axplat，而不是用户态或 rootfs。

## D1 axplat interface 缺口

StarryOS runtime 期望 platform 提供 console、power、irq、init、time 等接口。早期 D1 路径只让 `lichee-d1` feature 进入 StarryOS entry，但没有完整替换 QEMU axplat，也没有补齐必要接口，因此出现过链接缺 `__IrqIf_register`、`__IrqIf_set_enable`、`__IrqIf_handle` 等问题。

根因是 feature 与 platform package 不等价。必须通过 `MYPLAT=axplat-riscv64-lichee-d1`、D1 `axconfig.toml`、platform interface 实现共同切换，不能只靠 StarryOS 内部 entry 分支。

D1 init 落到 `crates/axplat-riscv64-lichee-d1/src/init.rs:1-30`：early init 初始化 trap、console、time；later init 按 feature 初始化 IRQ。

axplat 缺口属于链接和接口层问题，发生在运行前。不能靠真板日志定位，只能靠构建错误和 platform interface 边界来修。

## C906 页表属性问题

Smoke 阶段最关键的运行时 fault 是 `Store/AMO access fault`，符号化后指向 `percpu::imp::init` 中对 `.bss` 的 AMO 写。fault 不是 UART、USB、SD 卡或 rootfs 问题，而是 D1/C906 页表属性缺少 T-Head normal-memory `SH|B|C` bits。

修复须分两步，都需要 C906 属性：

1. early DDR identity/high-half mapping
2. 最终 kernel address space

只修 early mapping，系统会推进到更后面，但仍可能在最终页表阶段访问全局数据时 fault。

C906 memory attribute 是 D1 平台正确性的必要条件。遇到 AMO fault 时应先符号化 EPC/TVAL，确认是否页表属性，再查外设。

## QEMU 设备假设泄漏

Smoke 阶段必须禁用 fs、block、net、display、axdriver、PCI、task-ext 等与最小启动无关的模块。D1 没有 QEMU virtio block，也没有 PCI ECAM。若沿用 QEMU feature set，会出现 `No block device found!`、缺失 `PCI_ECAM_BASE` / `PCI_RANGES` / `PCI_BUS_END`，或访问 `phys_to_virt(0)`。

D1 axconfig 中 `virtio-mmio-ranges = []` 是关键细节，证据见 `crates/axplat-riscv64-lichee-d1/axconfig.toml:17-28`。不能用 `[[0,0]]` 占位——这会被驱动当成有效 MMIO range，并访问物理地址 0。

Smoke 策略：只打开能证明 boot 的功能。所有 QEMU 专有设备假设都应延后，直到有对应 D1 驱动或最小替代路径。

## 问题边界表

表 2 将 smoke 前问题与"不应优先排查的方向"对应起来，用于防止在错误层级消耗时间。

| 症状 | 高概率根因 | 不应优先归因 |
|---|---|---|
| boot 写入失败 | 镜像超过 boot 分区 | UART、页表、rootfs |
| U-Boot 后无 StarryOS | boot image / axplat 入口 | benchmark、syscall |
| 链接缺 platform symbol | D1 axplat interface 缺失 | 真板硬件损坏 |
| AMO fault | C906 PTE 属性 | USB、SD 卡 |
| `No block device found!` | 误启用 block/fs | boot image header |
| PCI 常量缺失 | axdriver default PCI | D1 PLIC |
| fault VA `0xffffffc000000000` | `virtio-mmio-ranges = [[0,0]]` | UART base 错 |

Smoke 前问题的共同规律：先削减运行面，再逐层打开。只有最小路径稳定后，才应讨论 PLIC、TTY、benchmark、rootfs。
