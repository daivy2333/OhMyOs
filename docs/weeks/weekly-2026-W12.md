# W12 - 本周做k3的探索工作

**周期**：2026-09-05 ~ 2026-09-12
**仓库**：[`daivy2333/k3`](https://github.com/daivy2333/k3)（9 个 commit）

> W11 在 QEMU 上把故障恢复语义收口后，W12 转向 K3 真板的物理接入和资料沉淀。三件事：去进迭时空官网查 K3 资料和工具、自购电源/按钮/杜邦线接板、把官网资料按 10 个主题沉淀到新建的 `daivy2333/k3` 仓。板子上电正常进入系统，串口没出现社区报告的输出问题。本周没有任何 StarryOS 产品代码提交。

## 真板首次上电

进迭时空官网的 K3 资料页（[source](https://www.spacemit.com/community/document/info?lang=zh&nodepath=hardware/key_stone/k3/k3_docs)）查了工具清单和引脚定义，然后做了很多记录。

自购了三样：电源、按钮、杜邦线。接板时**没用包装自带的杜邦线**，改用之前荔枝派项目剩下来的。

通电后串口直接拿到启动日志，工作正常

## 资料搬运：新建 k3 文档仓

新建 [`daivy2333/k3`](https://github.com/daivy2333/k3)（本地 `/home/daivy/projects/serial/work/k3`），把官网资料按主题切到 10 个子目录：

- `platform/`：K3 SoC 概述、平台控制资源、CoM260 板级资源
- `boot/`：启动链、可证镜像、DTS 候选
- `serial/`：17 个 UART 实例、CoM260 UART0 链路
- `dma/`：DMA 与内存归属、cache/PMA/地址翻译
- `interrupts/`：AP CLINT/APLIC/IMSIC、RP PLIC/SysTimer/MSIP、mailbox
- `network/`：GMAC PHY、DWMAC5 descriptor/IRQ
- `amp/`：AP/RP 共享内存、RPC ring
- `storage/`：QSPI/SPI/SDHCI、UFS
- `buses/`：I2C/USB、PCIe/CAN
- `reference/`：来源覆盖表、已知缺口、术语表、文档模板、刷新指南

`reference/source-coverage.md` 登记了 70 个唯一 URL，按 `主题位置 / 优先级 / 聚合状态 / 源端修订 / 观察日期` 维护。`reference/known-gaps.md` 登记了 G1~G12 共 12 类缺口，其中 G3/G4/G5 状态为 `partial`，每项都记了"禁止推断"和解除条件。

W12 走的是 OpenSpec 变更流：9/4 scaffold 文档基础 change，9/5 收尾；之后按 MS03~MS09 顺序建每条子系统基线 change。

## 提交记录（`daivy2333/k3`）

| 日期 | 提交 | 内容 |
|---|---|---|
| 9/4 | [`1f572bb`](https://github.com/daivy2333/k3/commit/1f572bb) | scaffold K3 文档基础 change |
| 9/5 | [`5731629`](https://github.com/daivy2333/k3/commit/5731629) | 收尾 doc-foundation change，同步基线 |
| 9/8 | [`bcdb076`](https://github.com/daivy2333/k3/commit/bcdb076) | MS03：板卡与启动链基线 |
| 9/8 | [`115f387`](https://github.com/daivy2333/k3/commit/115f387) | MS04：平台与 UART 基线 |
| 9/9 | [`b484827`](https://github.com/daivy2333/k3/commit/b484827) | MS05：中断、时间与 mailbox 基线 |
| 9/10 | [`8c69e9f`](https://github.com/daivy2333/k3/commit/8c69e9f) | MS06：DMA 与内存归属基线 |
| 9/10 | [`e81bdfc`](https://github.com/daivy2333/k3/commit/e81bdfc) | MS07：GMAC 与网络基线 |
| 9/10 | [`820535c`](https://github.com/daivy2333/k3/commit/820535c) | MS08：AMP RPC ring 基线 |
| 9/11 | [`c2d4387`](https://github.com/daivy2333/k3/commit/c2d4387) | MS09：存储控制器基线 |

## 下周

- 等待买的线材，然后继续探索，应该会做一些真板测试

## 参考

- [K3 资料仓（daivy2333/k3）](https://github.com/daivy2333/k3)
- [K3 文档入口](https://github.com/daivy2333/k3/blob/main/docs/index.md)
- [进迭时空 K3 资料源](https://www.spacemit.com/community/document/info?lang=zh&nodepath=hardware/key_stone/k3/k3_docs)
