# Lichee RV Dock 适配前期准备

> 范围：从拿到 Lichee RV Dock 到 StarryOS smoke 适配前，应收集和确认的硬件、启动、工具链与测试资料。
> 日期：2026-06-30
> 关联：`docs/licheerv-dock-bringup.md`、`docs/lichee-smoke-problems.md`、`docs/lichee-smoke-solutions.md`。

## 目标与边界

目标：建立可重复的真板 bring-up 事实库，回答四类问题：

- 板子如何启动
- 内核镜像如何被 U-Boot 加载
- 串口如何输出
- StarryOS 最小平台配置需要哪些硬件参数

Lichee RV Dock 使用 Allwinner D1 / XuanTie C906，单核 RISC-V。适合验证真板启动链、UART、PLIC、用户态 benchmark 交付流程。不适合 Q17 SMP / 多核内存序的最终验证。

VisionFive2 可复用采集方法；D1 的地址、IRQ、PTE 属性、boot image 细节不能复用。没有事实库的项，后续 fault 易被误判为"OS 问题"。

## 硬件与官方系统准备

第一步：让官方 Linux 稳定启动，并确认串口日志、TF 卡、U 盘、boot 分区操作路径是否正常。官方系统的价值在于提供已知可工作的 U-Boot、分区布局、串口参数和基本设备树线索，而不是要求 StarryOS 复用 Linux 的驱动配置。

| 项目 | 结论或动作 | 用途 |
|---|---|---|
| 官方镜像 | `LicheeRV_Tina_hdmi_8723ds.img` | 建立已知可启动基线 |
| 串口 | 115200 bps，3.3V TTL | 捕获 U-Boot、OpenSBI、StarryOS 输出 |
| boot 分区 | 约 `10.1M` | 判断 StarryOS boot image 是否可写入 |
| boot 设备 | `/dev/by-name/boot` | 官方 Linux 下备份和替换 |
| 外接存储 | `/mnt/exUDISK` | 传入 StarryOS boot image、保存备份 |

串口参数已稳定为 115200 bps；boot 替换依赖 `/dev/by-name/boot` 与 USB 外接存储路径。

官方 Linux 在此阶段是采集和替换环境，不是 StarryOS 运行环境。替换 boot 前必须先备份，建议使用带时间戳的文件名，避免覆盖唯一恢复点。

```bash
dd if=/dev/by-name/boot of=/mnt/exUDISK/boot-official-backup-$(date +%Y%m%d-%H%M%S).img bs=1M
sync
```

## 必采参数清单

采集须覆盖：boot image、内存布局、UART、PLIC、timer、分区大小、构建限制。D1 平台描述落在 `kernel/src/platform/lichee_d1.rs:19-47`，记录 RAM、kernel load、UART、PLIC、Android boot image 类型。

| 参数 | 当前值 | 代码或配置位置 |
|---|---|---|
| RAM base | `0x40000000` | `kernel/src/platform/lichee_d1.rs:24-27` |
| RAM size | `512 MiB` | `crates/axplat-riscv64-lichee-d1/axconfig.toml:5-8` |
| kernel load | `0x40200000` | `kernel/src/platform/lichee_d1.rs:28-31` |
| kernel vaddr | `0xffffffc040200000` | `crates/axplat-riscv64-lichee-d1/axconfig.toml:9-10` |
| UART0 base | `0x02500000` | `kernel/src/platform/lichee_d1.rs:32-39` |
| UART IRQ | `18` | `crates/axplat-riscv64-lichee-d1/axconfig.toml:26-28` |
| UART access | stride 4 / U32 | `kernel/src/platform/lichee_d1.rs:32-39` |
| PLIC base | `0x10000000` | `kernel/src/platform/lichee_d1.rs:40-42` |
| timer | SBI timer | `kernel/src/platform/lichee_d1.rs:43` |
| boot image | Android boot image | `kernel/src/platform/lichee_d1.rs:44-46` |

上表是 smoke 适配的输入。某类信息缺失时，应先补采集，再写平台代码。

板级参数应进入 platform descriptor 和 axconfig，避免散落在驱动初始化里。Q18/Q19 的一个关键经验：先集中事实，再写平台代码。

## 工具链与构建准备

工具链分两类：

1. StarryOS 内核构建工具链
2. 用户态 benchmark 交叉编译工具链

内核构建必须关闭 DWARF，否则 boot image 曾达 `25.6M`，超出约 `10.1M` 的 boot 分区。当前 Makefile 的 Lichee 目标均显式传入 `DWARF=n`，证据见 `Makefile:60-82`。

| 命令 | 输出 | 用途 |
|---|---|---|
| `make lichee` | `starry-lichee-boot.img` | early smoke |
| `make lichee-kbench` | `starry-lichee-kbench-boot.img` | kernel benchmark |
| `make lichee-userbench` | `starry-lichee-userbench-boot.img` | embedded user benchmark |

用户态 benchmark 必须编译为静态 RISC-V ELF，禁止 PIE。embedded loader 不处理 relocation，因此用 `-static -no-pie -fno-pie -s`，并用 `readelf -r` 确认无 relocation。

```bash
export PATH=/opt/musl/riscv64-linux-musl-cross/bin:$PATH
riscv64-linux-musl-gcc -static -no-pie -fno-pie -Os -s \
  -o kernel/resources/benchmark.elf tests/benchmark.c
file kernel/resources/benchmark.elf
readelf -h kernel/resources/benchmark.elf | grep 'Type:'
readelf -r kernel/resources/benchmark.elf
```

构建准备的关键：产物满足 boot 分区尺寸、Android boot header、embedded ELF loader 三项约束。

## 证据保存规范

真板适配必须保存原始串口输出。同一症状可能对应不同阶段：U-Boot 后无输出、StarryOS 早期 panic、最终页表 fault、userbench 卡住 `tcdrain`，需不同证据才能定位。

建议按阶段保存到 `.claude/analysis/lichee/`，文档只引用摘要。当前 q19B 原始证据已以 `kbench`、`userbench` 等文件形式保存；bring-up 总结写入 `docs/licheerv-dock-bringup.md`。

```bash
mkdir -p .claude/analysis/lichee
# 建议命名：
# q19-smoke-YYYYMMDD.txt
# q19b-kbench-YYYYMMDD.txt
# q19b-userbench-YYYYMMDD.txt
```

日志是排障证据，文档是结构化结论。两者都要保留：日志防误记，文档防经验遗失。

## 复用到新板的检查清单

Lichee 前期准备经验可复用到 VisionFive2 等新板，但只能复用流程，不能复用 D1 参数。新板应重新确认：boot 方式、load address、RAM、UART base、IRQ、MMIO width、PLIC/timer、页表属性、官方恢复方法、boot 分区大小。

| 类别 | 必要性 | 说明 |
|---|---|---|
| 官方系统可恢复 | 必要 | 防止 boot 分区替换后无法回退 |
| 串口稳定输出 | 必要 | 所有 early bring-up 都依赖串口 |
| boot image 格式 | 必要 | 决定打包和 load address |
| RAM / UART / PLIC / timer | 必要 | 最小 axplat 和 early console 所需 |
| MMIO access width | 必要 | 决定是否能复用 NS16550 byte backend |
| rootfs / SDMMC | 可选 | 不应阻塞第一轮 smoke |
| benchmark payload | 可选 | Q19B 采用 embedded ELF 绕开 rootfs |

完成标准：能解释每个早期地址和启动假设的来源。仅凭印象填写的项尚未达到写平台代码的条件。
