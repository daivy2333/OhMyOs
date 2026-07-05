# Lichee RV Dock Smoke 解决路径

> 范围：从无法启动到 Q19 smoke 输出 `[starry-d1] smoke complete, halting.` 的修复路径。
> 日期：2026-06-30
> 关联：`docs/lichee-smoke-problems.md`、`docs/lichee-adaptation-prework.md`、`docs/licheerv-dock-bringup.md`。

## 解决原则

Smoke 的解决原则：建立最小可启动流程——官方 U-Boot 加载 Android boot image，D1 axplat 初始化 trap/time/early console，StarryOS 打印平台信息并停机。该路径不要求 fs、task、block、网络、display、用户态 benchmark 成立。

代码证据分布在四处：

- D1 平台描述：`kernel/src/platform/lichee_d1.rs:19-47`
- D1 axconfig：`crates/axplat-riscv64-lichee-d1/axconfig.toml:5-28`
- Makefile Lichee targets：`Makefile:60-82`
- D1 init：`crates/axplat-riscv64-lichee-d1/src/init.rs:1-30`

四处共同决定 smoke 是否能进入 StarryOS。

## 产物尺寸与 Android boot image

第一项修复：把 StarryOS 产物变为官方 U-Boot 可接受且 boot 分区可容纳的 Android boot image。`make lichee` 已显式传入 `DWARF=n` 并调用 `tools/android_boot_image.py pack`，证据见 `Makefile:60-66`。

当前 smoke 产物关键字段：

- `kernel_size = 118976` bytes
- `kernel_addr = 0x40200000`
- `page_size = 2048`
- `name = d1-nezha`

字段与官方 boot image 对齐后，boot 分区替换才进入可重复阶段。

```bash
PATH=/opt/musl/riscv64-linux-musl-cross/bin:$PATH make lichee
python3 tools/android_boot_image.py inspect starry-lichee-boot.img
```

关闭 DWARF 与 Android boot image 打包是 smoke 的第一道 gate。没有这个 gate，后续代码是否正确都无法被真板验证。

## D1 platform descriptor 与 axconfig

第二项修复：建立 D1 平台事实的单一入口。

- `kernel/src/platform/lichee_d1.rs:19-47` 描述 RAM、kernel load、UART、PLIC、timer、boot kind
- `crates/axplat-riscv64-lichee-d1/axconfig.toml:5-28` 把这些事实提供给 axplat 构建

关键字段：

- `phys-memory-base = 0x4000_0000`
- `kernel-base-paddr = 0x4020_0000`
- `uart-paddr = 0x0250_0000`
- `uart-irq = 18`
- `plic-paddr = 0x1000_0000`

`virtio-mmio-ranges = []` 必须保持为空，避免误探测不存在的 virtio 设备。

平台参数集中化避免驱动层继续硬编码板级常量。后续 VisionFive2 适配应先复制这个模式，不要复制 D1 的具体数值。

## D1 axplat 与 early init

第三项修复：让 StarryOS 真正链接 D1 axplat，而不是只在 StarryOS entry 内判断 `lichee-d1` feature。Makefile 中的 `MYPLAT=axplat-riscv64-lichee-d1` 和 `PLAT_CONFIG=.../axconfig.toml` 是必要条件，证据见 `Makefile:60-61`。

D1 axplat 的 early init 只做 trap、early console、early time；later init 按 feature 初始化 IRQ，证据见 `crates/axplat-riscv64-lichee-d1/src/init.rs:7-22`。该分层保证 smoke 模式在没有完整 IRQ、task、fs 的情况下仍可输出并停机。

## C906 页表属性修复

第四项修复：为 D1/C906 的页表映射添加 T-Head normal-memory 属性。历史 fault 指向 `percpu::imp::init` 的 AMO 写，说明普通内存属性不满足 C906 对 AMO/cacheable/shareable 的要求。

修复分两步：

1. early DDR identity/high-half mapping 加 `SH|B|C` bits
2. 最终页表启用 `page_table_entry/xuantie-c9xx`

只修第一步时，系统能推进到更后面，但仍可能在最终 address space 访问全局数据时 fault。该修复属于平台内存模型修复，不是业务逻辑修复，必须覆盖 early mapping 和 final mapping 两层。

## Smoke feature gate

第五项修复：严格限制 smoke 模式的 feature。Q19a 只需要 boot、early console、SBI timer、halt；fs、net、display、axdriver、PCI、task-ext 均不应进入这条路径。该策略避免在无 SDMMC、无 PCI、无 virtio 阶段出现错误扩散。

D1 smoke 运行结果：

```text
arch = riscv64
platform = riscv64-lichee-d1
target = riscv64gc-unknown-none-elf
build_mode = release
log_level = warn
backtrace = false
smp = 1

[starry-d1] early boot
hart_id: unavailable in S-mode
sbi_version: 0.2
[starry-d1] smoke complete, halting.
```

该 gate 的价值：把"启动平台"从"完整 OS"中剥离出来。边界越清楚，后续 benchmark 阶段的问题越容易定位。

## 烧录与恢复流程

写入前检查镜像大小和 Android boot header；写入时先备份官方 boot；失败后从备份恢复。命令在官方 Linux 中执行。

```bash
ls -lh /mnt/exUDISK/starry-lichee-boot.img
dd if=/dev/by-name/boot of=/mnt/exUDISK/boot-official-backup.img bs=1M
sync
dd if=/mnt/exUDISK/starry-lichee-boot.img of=/dev/by-name/boot bs=1M conv=fsync
sync
reboot -f
```

恢复官方 boot：

```bash
dd if=/mnt/exUDISK/boot-official-backup.img of=/dev/by-name/boot bs=1M conv=fsync
sync
reboot -f
```

真板 smoke 的工程质量取决于可重复恢复。没有恢复路径，不应频繁替换 boot 分区。

## 解决链总结

表 1 是 smoke 阶段最终解决链，按依赖顺序排列。后续新板 bring-up 可按同样顺序检查。

| 顺序 | 修复 | 验收 |
|---|---|---|
| 1 | `DWARF=n` + Android boot image | boot image 小于 boot 分区 |
| 2 | D1 platform descriptor | 参数不再散落在驱动里 |
| 3 | D1 axplat + axconfig | 构建链接 D1 platform |
| 4 | early console | StarryOS 能打印 early log |
| 5 | C906 PTE 属性 | AMO fault 消失 |
| 6 | smoke feature gate | 无 fs/block/PCI 干扰 |
| 7 | halt after smoke | 串口输出稳定终止 |

Smoke 成功标准：短而确定的串口输出，而不是系统功能越多越好。Q19 的修复经验应作为后续所有真板适配的前置模板。
