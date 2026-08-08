# Lichee Q19B kbench/userbench 问题与解决

> 范围：Q19 smoke 完成后，在 Lichee RV Dock 上推进 async UART kbench/userbench 时遇到的问题、解决路径与性能结果。
> 日期：2026-06-30
> 关联：`docs/benchmark-report-async.md`、`docs/licheerv-dock-bringup.md`、`docs/uart-async-learning-map.md`。

## 阶段目标

Q19B 的目标：在 Lichee RV Dock 上获得实测 async UART benchmark 数据，并以此证明内核可工作，而非仅停留在"能启动"。

该目标要求同时打通：D1 async UART、PLIC IRQ 18、kernel ring benchmark、`/dev/console`、TTY、syscall、embedded user ELF loader、`tcdrain`、FIONBIO。

Q19B 三种模式：

- `lichee-d1`：保持 smoke 回归
- `lichee-d1-kbench`：跑内核 benchmark 后停机
- `lichee-d1-userbench`：挂载最小伪文件系统并运行内嵌 `benchmark.elf`

入口证据在 `kernel/src/entry.rs:145-239`，构建证据在 `Makefile:68-82`。Q19B 验收面明显大于 Q19 smoke：单个 kbench 成功不能代表用户态 benchmark 成功。

## 问题一：QEMU UART 模型不能用于 D1

QEMU 路径按 NS16550 byte MMIO 访问 UART。D1 的 DW APB UART 需要 stride 4 / 32-bit MMIO。若复用 QEMU 的 `Uart16550<MmioBackend>`，raw LSR probe、RBR/THR、IER/IIR/LSR 访问宽度都会错误。

解决方案：在 `kernel/src/drivers/d1_uart.rs` 实现 D1 专用 `ArceOsD1UartPort`，通过 `read_reg(offset)` / `write_reg(offset, val)` 做 `offset * stride` 的 U32 volatile 访问。关键寄存器偏移：

- RBR/THR=0
- IER=1
- IIR/FCR=2
- MCR=4
- LSR=5

证据见 `kernel/src/drivers/d1_uart.rs:17-39` 和 `kernel/src/drivers/d1_uart.rs:67-87`。

D1 支持不是改 UART base address，而是换 MMIO 访问模型。该问题须在进入 IRQ 和 TTY 前解决。

## 问题二：kbench 与 userbench feature 边界混乱

早期 `lichee-d1-userbench` 继承 kbench-only feature，导致 userbench 所需的 `file`、`mm`、`pseudofs`、`task`、`ASYNC_TTY` 等模块被排除。若启用完整 QEMU feature，又会带入 PCI、virtio、display、net 等 D1 当前不具备的假设。

按职责拆分 feature：

- `lichee-d1-async-uart`：表示 D1 UART + PLIC 能力
- `lichee-d1-kbench` / `lichee-d1-userbench`：表示不同运行模式

`kernel/src/entry.rs:147-239` 展示 kbench/userbench 共用 D1 初始化，再按 feature 分支进入内核 benchmark halt 或用户 benchmark 进程。

feature 是架构边界，不只是编译开关。硬件能力与运行模式混用，会让某个模式为了通过编译而破坏另一个模式。

## 问题三：axfs-ng 间接启用 PCI

userbench 需要 `/dev/console`，依赖 `pseudofs::mount_all()` 和 `axfs::FS_CONTEXT`。引入 `axfs` 后，`axfs-ng` 间接依赖 `axdriver`。若未显式启用 `bus-mmio`，`axdriver` 的 build script 默认输出 `cfg(bus="pci")`，导致 D1 缺少 `PCI_ECAM_BASE`、`PCI_RANGES`、`PCI_BUS_END`。

解决方案：本地 patch `crates/axfs-ng/Cargo.toml`，把 `axdriver` 依赖改为 `default-features = false, features = ["block", "bus-mmio"]`，证据见 `crates/axfs-ng/Cargo.toml:16-20`。

该 patch 只为 D1 userbench 解开最小 VFS/TTY 路径，不代表已实现 SDMMC/rootfs parity。

`cargo tree` 看不到 `bus-pci` feature 并不代表不编译 PCI 路径；`axdriver` 的自定义 cfg 由 build script 决定。遇到此类问题应检查 `target/*/build/axdriver-*/output`。

## 问题四：embedded benchmark ELF 交付

D1 当前没有 StarryOS SDMMC/rootfs 路径。若坚持从文件系统加载 `/bin/benchmark`，block bring-up 会与 UART benchmark 纠缠。

Q19B 选择：先把 `tests/benchmark.c` 编译为静态 RISC-V ELF，并通过 `include_bytes!` 嵌入 kernel。

- `kernel/src/mm/loader.rs:348-445` 实现 `load_embedded_user_app()`，从 byte slice 解析 ELF、映射段、创建用户栈。
- `kernel/src/entry.rs:180-191` 用 `AlignedBytes` 包装 `include_bytes!("../resources/benchmark.elf")`，避免 ELF 头对齐问题，再调用 embedded loader。

embedded ELF 必须是 `ET_EXEC` 且没有 relocation。编译命令：

```bash
export PATH=/opt/musl/riscv64-linux-musl-cross/bin:$PATH
riscv64-linux-musl-gcc -static -no-pie -fno-pie -Os -s \
  -o kernel/resources/benchmark.elf tests/benchmark.c
readelf -h kernel/resources/benchmark.elf | grep 'Type:'
readelf -r kernel/resources/benchmark.elf
```

embedded ELF 用于隔离 benchmark 目标与 SDMMC/rootfs 目标。它是 Q19B 的最小可测路径，不是长期 rootfs 方案。

## 问题五：tcdrain 卡住与 THRE 边沿丢失

userbench 首次推进到 `/dev/console` 后卡在第一轮 64B write 的 `tcdrain`。此时 write、TX ring、TTY、用户进程都已成立，问题在 drain completion：

- 等待者只覆盖 TX ring
- 没有覆盖 copier staged buffer
- 没有覆盖 UART TEMT 状态变化

D1 真板还暴露出 QEMU 没有的问题：PLIC 能进入 UART IRQ 18，但 IIR 多次为 `0xc1`（no-pending）；有效 THRE `0xc2` 只偶发。

解决方法分两处：

1. D1 `update_ier(THR_EMPTY)` 后立即检查 LSR，若 THRE/TEMT 已为真，则软件唤醒 `TX_WAKER` / `DRAIN_WAKER`。证据见 `kernel/src/drivers/d1_uart.rs:157-175`。
2. ISR 遇到 no-pending 时也基于 LSR 补 wake。证据见 `kernel/src/drivers/d1_uart.rs:192-205`。

真板 `tcdrain` 不能只依赖未来 THRE 中断。正确模型是 interrupt-driven 加 state-driven，两者缺一不可。

## 问题六：串口输出斜行与退出日志插队

userbench 跑通后，串口输出出现斜行：终端下移但不回行首。同时内核的 `benchmark exited` 日志可能在用户态最后几行前插队。

根因：

- benchmark 输出只有 LF，串口终端需要 CRLF
- 用户态 stdout 退出前没有完全 drain

解决方案分两层：

1. TTY 层按默认 termios `OPOST|ONLCR` 做 LF→CRLF，证据见 `kernel/src/pseudofs/dev/tty/mod.rs:107-145`。
2. benchmark 源码改为 CRLF，并在退出前执行 `fflush(stdout); tcdrain(STDOUT_FILENO);`。benchmark 测试主体见 `tests/benchmark.c:35-83`、`tests/benchmark.c:85-180`、`tests/benchmark.c:182-220`。

输出格式不是美观问题，而是证据质量问题。串口日志混乱会影响后续性能数据和故障定位的可信度。

## 性能结果

表 1 是 Q19B userbench 在 Lichee RV Dock / UART0 115200 bps 下的真板结果。理论线速按 115200 bps、10 bit/byte 计算，为 11.52 KB/s。

| 测项 | 结果 | 解释 |
|---|---|---|
| 理论线速 | 11.52 KB/s | 115200 bps / 10 bit per byte |
| 64B TX | 1.01 KB/s，8.8% line rate | 每轮 `tcdrain`，固定开销主导 |
| 256B TX | 11.25 KB/s，97.7% line rate | 接近物理上限 |
| 1024B TX | 11.40 KB/s，98.9% line rate | 接近物理上限 |
| 4096B TX | 11.41 KB/s，99.0% line rate | 接近物理上限 |
| 1B latency | avg 0.270 ms，P50 0.185 ms，P95 0.187 ms，P99 8.547 ms | P99 受调度/IRQ 尾延迟影响 |
| FIFO 16B | avg 1.564 ms，P50 1.513 ms | 约一个 FIFO 深度 |
| FIFO 32B | avg 2.927 ms，P50 2.876 ms | 约两个 FIFO 深度 |
| FIFO 48B | avg 4.293 ms，P50 4.242 ms | 约三个 FIFO 深度 |
| FIONBIO | `O_NONBLOCK` 与 `ioctl FIONBIO` 均 PASS | 无输入返回 `EAGAIN` |

数据说明：

- ≥256B 的吞吐受 115200 bps 物理线速主导，ring buffer 与 copier 未成为瓶颈
- 64B 低吞吐是测试方法导致的固定 drain 开销，不代表大包路径退化

Q19B 性能结论：大包接近线速，小包受每轮 drain 固定开销影响。该结论与 QEMU 高吞吐数据不能套用——QEMU 不仿真物理串口线延迟。

## 构建与烧录命令

表 2 给出 Q19B 的构建产物。写入前仍需确认 boot image 小于官方 boot 分区，并保留可恢复备份。

| 目标 | 输出 | 用途 |
|---|---|---|
| `make lichee-kbench` | `starry-lichee-kbench-boot.img` | 内核 ring benchmark |
| `make lichee-userbench` | `starry-lichee-userbench-boot.img` | 用户态 benchmark |

构建命令：

```bash
PATH=/opt/musl/riscv64-linux-musl-cross/bin:$PATH make lichee-kbench
PATH=/opt/musl/riscv64-linux-musl-cross/bin:$PATH make lichee-userbench
python3 tools/android_boot_image.py inspect starry-lichee-userbench-boot.img
```

官方 Linux 下替换 boot：

```bash
dd if=/dev/by-name/boot of=/mnt/exUDISK/boot-official-backup-$(date +%Y%m%d-%H%M%S).img bs=1M
sync
dd if=/mnt/exUDISK/starry-lichee-userbench-boot.img of=/dev/by-name/boot bs=1M conv=fsync
sync
reboot -f
```

命令流已稳定，但仍是 boot 分区替换实验。每次烧录前应确认镜像、备份、串口日志保存路径。

## 后续边界

Q19B 已完成 async UART 数据采集目标，但未完成 SDMMC/rootfs parity。若要从 TF 卡 rootfs 加载 benchmark，应作为 Q19C 或独立 milestone：先实现 D1 SDMMC/block，再恢复常规文件系统加载路径。

VisionFive2 不能继承 D1 的 UART base、IRQ、access width、boot image 格式。可复用的是分层方法：

- 先 smoke，再 kbench，再 userbench
- 先 embedded payload，再 rootfs parity
- 先证明 `tcdrain` 状态机，再比较性能

Q19B 完成标准已达成。后续工作应从"继续修 Lichee userbench"转为"选择新的明确目标"——SDMMC/rootfs parity 或 VisionFive2 真板验证。
