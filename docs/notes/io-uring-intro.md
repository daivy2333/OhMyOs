# io_uring 入门

**标签**：linux, io, async, kernel, syscall

> 面向零基础读者。从磁盘读取一个字节说起，逐步推导到 io_uring 的核心机制、内存布局、提交/收割流程与性能优势。

---

## 1. 一切的开始：程序想读一个文件

最简单的 C 程序：

```c
int fd = open("hello.txt", O_RDONLY);   // 打开文件
char buf[100];
int n = read(fd, buf, 100);              // 读 100 字节
printf("%s\n", buf);
```

这三行背后隐藏着**大量**你看不见的机制。

### 1.1 用户态 vs 内核态

CPU 执行程序时，有两种权限级别：

| 概念 | 含义 | 类比 |
|------|------|------|
| **用户态（User Space）** | 你的程序（main、printf 等）运行在这里，权限受限 | 普通员工 |
| **内核态（Kernel Space）** | 操作系统核心（文件系统、网络、进程调度）运行在这里，能直接操作硬件 | 公司管理层 + 后勤 |

普通员工不能直接去机房拔硬盘——你必须**提交申请**给后勤部门。这种"申请"，在操作系统里叫**系统调用（System Call）**。

`open`、`read`、`write`、`close` 全都是系统调用。它们**不是普通的函数**——它们会让 CPU 从用户态切换到内核态，让内核替你去访问磁盘、网卡等硬件。

### 1.2 文件描述符（File Descriptor, fd）

`open()` 返回的那个整数 `fd`，是一个**句柄**——你拿着这个数字，内核就知道你要操作哪个"打开的文件"。

类比：你在柜台填表，柜台给你一个**取件号码**。下次你拿这个号码，就能领到你要的"文件对象"。

每个进程默认有三个：

```
fd = 0  →  stdin  （标准输入，默认接键盘）
fd = 1  →  stdout （标准输出，默认接终端）
fd = 2  →  stderr （标准错误，默认接终端）
```

`open()` 会返回当前可用的最小号码（通常是 3）。

### 1.3 `read()` 实际做了什么

调用 `read(fd, buf, 100)` 时：

1. **CPU 切到内核态**（保护现场、记下"我刚才执行到哪"）
2. **内核找到 fd 对应的文件对象**（文件在磁盘哪个位置、权限是什么）
3. **内核向磁盘控制器发指令**："请把 hello.txt 的前 100 字节读上来"
4. **磁盘开始转动、磁头开始寻道**——这是**机械动作**，可能几毫秒到几十毫秒
5. **磁盘把数据放到内存**，内核把数据**拷贝**到你的 `buf`
6. **CPU 切回用户态**，返回实际读到的字节数

第 4 步是关键：**CPU 在磁盘转动时什么都干不了**。这叫**阻塞 I/O（Blocking I/O）**——你的程序"卡住"了，傻等。

---

## 2. 为什么需要异步 I/O

阻塞 I/O 在单任务里没问题。但如果你的程序要同时处理**一万个网络连接**呢？

```c
for (int i = 0; i < 10000; i++) {
    char buf[1024];
    int n = read(fd_array[i], buf, sizeof(buf));   // 阻塞
    process(buf);
}
```

第 0 个客户端不返回，第 1~9999 个就永远等不到。**不可行**。

### 2.1 早期解法：多线程 / 多进程

每来一个连接，开一个线程处理：

```
主线程：accept() 新连接 → 派给子线程
子线程 1：read() 客户端 1（阻塞，但只阻塞自己）
子线程 2：read() 客户端 2（阻塞，但只阻塞自己）
```

问题：

- 每个线程占用 **8 MB 左右栈空间**。1 万连接 = 80 GB 内存，崩。
- 线程切换（context switch）开销不小——CPU 要保存/恢复寄存器、刷新缓存。
- 内核要维护每个线程的状态。

### 2.2 改进：select / poll / epoll

聪明的工程师们想到：**别一个 fd 一个线程，让一个线程轮询多个 fd**。

#### `select`（1980 年代）

```c
fd_set readfds;
FD_ZERO(&readfds);
for (int i = 0; i < 10000; i++) FD_SET(fd_array[i], &readfds);

struct timeval tv = {5, 0};
int n = select(10001, &readfds, NULL, NULL, &tv);   // 阻塞直到有 fd 就绪
```

工作机制：

1. 把所有"想监听"的 fd 放进一个集合
2. `select()` 把**整个集合从用户态拷贝到内核态**
3. 内核**轮询**每一个 fd，看数据到了没
4. 没有就绪就**阻塞**
5. 有任何一个就绪，就返回，把**整个集合再拷回用户态**
6. 你遍历集合找出哪些 fd 就绪了

`select` 的问题：

- `fd_set` 大小有限制（通常 1024）
- **每次都要把整个集合在用户态和内核态之间来回复制**
- 内核每次都要**线性扫描**所有 fd——O(N) 复杂度

#### `poll`（1997）

把"位图"换成了"链表结构 `pollfd`"，突破了 1024 限制，但**仍然要把整个数组拷来拷去 + 线性扫描**。

#### `epoll`（Linux 2.6, 2002 前后）

`epoll` 是质的飞跃：

```c
int epfd = epoll_create1(0);                              // 创建一个 epoll 实例

struct epoll_event ev;
ev.events = EPOLLIN;
ev.data.fd = fd_array[0];
epoll_ctl(epfd, EPOLL_CTL_ADD, fd_array[0], &ev);          // 注册 fd（只拷一次）

while (1) {
    struct epoll_event events[100];
    int n = epoll_wait(epfd, events, 100, -1);             // 等就绪事件
    for (int i = 0; i < n; i++) {
        // 处理 events[i].data.fd
    }
}
```

关键改进：

| 改进 | 含义 |
|------|------|
| **注册一次，长期有效** | `epoll_ctl` 添加的 fd 在内核的**红黑树**里，不需要每次都传整个集合 |
| **就绪列表（ready list）** | 内核把"已经就绪"的 fd 放进链表，`epoll_wait` 只返回**就绪的** |
| **就绪回调（callback）** | 当某个 fd 数据到达时，内核通过回调把它加入就绪链表，**O(1)** |

`epoll` 至今仍是 Linux 高并发网络编程的事实标准。Redis、Nginx 都用它。

但 `epoll` 仍然有缺点：

1. **`epoll_wait` 本身还是系统调用**——每次都要进内核、出内核
2. **`epoll` 只是"通知机制"**，告诉你 fd 就绪了，你还得**自己再调一次 `read()`**——又一次系统调用
3. **数据要拷贝两次**：磁盘 → 内核页缓存 → 用户缓冲区
4. **缓冲区管理复杂**：每个连接要自己维护收发缓冲区

---

## 3. io_uring 的诞生

2019 年，Linux 5.1 引入 **io_uring**，由 Jens Axboe（也是 `fio`、`blk-mq` 的作者）设计。目标：

> **彻底消除系统调用次数 + 消除数据拷贝 + 共享内存通信**

核心思想一句话：

> **让用户态和内核态共享一块环形缓冲区（ring buffer），通过它提交 I/O 请求和收割 I/O 结果，全程不需要系统调用介入数据传递。**

### 3.1 三大核心组件

| 组件 | 缩写 | 作用 |
|------|------|------|
| **提交队列（Submission Queue）** | SQ | 你把想做的 I/O 请求（"读这个文件"、"写那块内存"）写到这里 |
| **完成队列（Completion Queue）** | CQ | 内核把做完的 I/O 结果（"读到了 100 字节"）放到这里 |
| **环形缓冲区（Ring Buffer）** | Ring | SQ 和 CQ 各自的存储结构，"环形"指首尾相接、可复用 |

环形缓冲区是经典的并发数据结构：一个固定大小的数组，有两个指针——`head`（写位置）和 `tail`（读位置）。`tail` 追上 `head` 就满了，`head` 追上 `tail` 就空了。空出来的位置可以继续复用。

### 3.2 与传统 I/O 的对比

| 维度 | 阻塞 I/O / epoll | io_uring |
|------|------------------|----------|
| 每次操作需要系统调用次数 | read = 1，epoll = 1~2 | 提交和收割**各 1 次**（甚至 0 次，启用 SQPOLL 后） |
| 数据从内核到用户态 | 拷贝 | **不拷贝**（共享内存） |
| 内核到用户态的"通知"方式 | 事件回调 + 二次系统调用 | **CQ 直接写结果**到共享内存 |
| 批量提交 | 需多次系统调用 | **一次提交多个 SQE** |

---

## 4. io_uring 的内存布局（核心中的核心）

io_uring 通过 **`mmap()`** 把内核缓冲区**直接映射**到用户进程的虚拟地址空间。这样：

- 用户程序读写这段内存，**不需要系统调用**——CPU 还是用户态，但访问的是**同一块物理内存**
- 内核往这段内存写完成结果，**也不需要"通知"用户态**——它已经写好了，你随时能读到

io_uring 的"完整内存布局"通过**三次 mmap** 得到：

```
进程虚拟地址空间
┌────────────────────────────────────┐
│                                    │
│  1. sq_ring（提交队列头）           │  ← io_uring_setup 返回的 mmap
│     - SQ head, tail, ring_mask     │
│     - 各数组长度                    │
│     - flags                        │
│                                    │
│  2. sqes（SQE 数组）                │  ← 第二次 mmap
│     - 每个元素是一个 io_uring_sqe   │
│     - 长度 = SQ entries × sizeof(sqe)
│                                    │
│  3. cq_ring（完成队列）             │  ← 第三次 mmap
│     - CQ head, tail, ring_mask     │
│     - 实际 CQE 数据                │
│                                    │
└────────────────────────────────────┘
```

为什么要分三个？历史遗留 + 灵活性——让 SQ 和 CQ 可以独立大小、独立对齐。一般使用者不必深究，库（liburing）会帮你算好偏移。

### 4.1 关键字段：head、tail

这是 io_uring 最精妙的设计：

- **SQ 的 `head` 和 `tail`**：用户态**只写 `tail`**，内核**只读 `tail`、写 `head`**
- **CQ 的 `head` 和 `tail`**：内核**只写 `tail`**，用户态**只读 `tail`、写 `head`**

规则（简化版）：

```
提交一个 SQE：
    sqe_array[tail & mask] = 你的请求
    写内存屏障（store barrier）
    SQ.tail++      ← 让内核看到"我又加了一个"

收割一个 CQE：
    读内存屏障（load barrier）
    while (head != CQ.tail) {
        cqe = cq_array[head & mask]
        处理 cqe
        head++      ← 让内核看到"我处理完了"
    }
    CQ.head = head
```

这就是**无锁环形队列**（lock-free ring buffer）的经典做法：**单生产者单消费者**场景下，靠 head/tail 指针 + 内存屏障，不需要任何锁。

内存屏障（memory barrier / fence）的作用：防止 CPU / 编译器把你的写入"重排"到读取之前。先记住一句话：**它是"告诉 CPU 不许乱序"的指令**。具体为什么需要，到 6.4 节再展开。

---

## 5. SQE 与 CQE：你要"告诉"内核做什么、内核"告诉"你做了什么

### 5.1 SQE（Submission Queue Entry，提交条目）

你想让内核做什么，就填一个 SQE：

```c
struct io_uring_sqe {
    __u8    opcode;       // 操作类型：读、写、accept、send...（IORING_OP_*）
    __u8    flags;        // 额外标志
    __u16   ioprio;       // I/O 优先级
    __s32   fd;           // 文件描述符（如果是文件 I/O）
    __u64   off;          // 文件内偏移
    void   *addr;         // 缓冲区地址
    __u32   len;          // 长度
    __u64   user_data;    // 【关键】用户自定义 ID，收 CQE 时能认回
    // ... 其他字段（链接、地址、splice 等）
};
```

最重要的字段：

- **`opcode`**：操作类型。例如 `IORING_OP_READV` = 读 scatter/gather，`IORING_OP_ACCEPT` = 接受连接
- **`user_data`**：**你设的标签**。提交时设 0x12345，完成时内核会原样回填。是"识别是哪个请求完成了"的关键
- **`fd` / `addr` / `len`**：操作参数

### 5.2 CQE（Completion Queue Entry，完成条目）

内核完成一个 I/O 后，在 CQ 写一个 CQE：

```c
struct io_uring_cqe {
    __u64   user_data;    // 对应 SQE 里设的 user_data
    __s32   res;          // 结果：成功是字节数，失败是 -errno
    __u32   flags;
};
```

`res` 是关键：

- 成功：`res = 实际读到的字节数`（例如 1024）
- 失败：`res = -EAGAIN`、`-EBADF`、`-EIO` 等

注意 **`user_data` 是 64 位的**，你可以把指针塞进去——把请求的内存地址写进去，CQE 回来时直接 cast 回原类型。

---

## 6. 完整工作流程

### 6.1 五步走

```
步骤 1: io_uring_setup(entries, params)
   ↓
   内核创建 io_uring 实例，返回 fd

步骤 2: 三个 mmap() 拿到 SQ/CQ 共享内存地址

步骤 3: 准备 SQE → 放到 SQ 数组 → 更新 SQ.tail

步骤 4: io_uring_enter(ring_fd, to_submit, min_complete, flags, sig)
   ↓
   【提交】告诉内核："请处理 SQ 里的 N 个请求"
   【收割】告诉内核："我等至少 M 个完成"
   ↓
   内核处理请求，结果写入 CQ

步骤 5: 遍历 CQ → 处理每个 CQE → 更新 CQ.head
```

### 6.2 一个具体例子：读文件

```c
// 伪代码：io_uring 版的 "读 100 字节"
struct io_uring ring;
io_uring_queue_init(8, &ring, 0);              // 创建，最多 8 个 in-flight 请求

int fd = open("hello.txt", O_RDONLY);
char buf[100];

struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);   // 拿一个空 SQE
io_uring_prep_read(sqe, fd, buf, 100, 0);             // 填好 opcode=READ, fd, buf, len, off
sqe->user_data = 0xCAFE;                              // 打标签
io_uring_submit(&ring);                               // 步骤 4：提交

// 收割
struct io_uring_cqe *cqe;
io_uring_wait_cqe(&ring, &cqe);                       // 阻塞等至少 1 个完成
if (cqe->res > 0) {
    // 读到了 cqe->res 字节，buf 里就是数据
}
io_uring_cqe_seen(&ring, cqe);                        // 步骤 5：告诉内核"我处理完了"

io_uring_queue_exit(&ring);                           // 清理
close(fd);
```

看起来和 `read()` 差不多？区别在哪？

- `io_uring_submit()` 可以**一次提交多个 SQE**——内核**批处理**它们，效率比多次 `read()` 高得多
- **关键性能技巧**：提前准备好一批 SQE，最后**只调用一次** `io_uring_submit()`，内核一口气做完
- 启用 **SQPOLL 模式**（kernel polling）后，甚至**不需要 `io_uring_submit()`**——内核有专门的 worker 线程轮询 SQ 数组

### 6.3 一个更实际的例子：echo 服务器

echo 服务器 = 把客户端发来的数据原样发回去。

io_uring 版本思路：

```c
while (1) {
    // 1) 收割所有完成的 CQE
    // 2) 对每个 CQE：
    //    - 如果是 ACCEPT 完成 → 拿到新 fd，注册一个 READ SQE
    //    - 如果是 READ 完成 → 处理数据，注册一个 WRITE SQE 把数据发回去
    //    - 如果是 WRITE 完成 → 注册下一个 READ SQE

    // 3) 一次性提交所有准备好的 SQE

    // 4) io_uring_wait_cqe_timeout() 等一会儿
}
```

每条连接的状态可以用 `user_data` 编码（高位是连接 id，低位是状态码），或者单独维护一张表。

### 6.4 关于内存屏障（更细致一点）

为什么 `SQ.tail++` 之前需要 `smp_store_release`，`CQ.head = ...` 之后需要 `smp_store_release`？

考虑**现代 CPU 的乱序执行**：

```
时间线（理想顺序）:
    CPU0（用户态）：写 SQE 数据 → 写 SQ.tail++
    CPU1（内核态）：读 SQ.tail → 读 SQE 数据

如果 CPU 乱序：
    CPU0 把 SQ.tail++ 先做了，SQE 数据还没写完
    CPU1 看到 SQ.tail 变了，去读 SQE，但读到的是旧数据或垃圾
```

`store-release` / `load-acquire` 指令保证：

- **release 之后的写入，一定在 release 之前对其他 CPU 可见**
- **acquire 之后的读取，一定能看到 acquire 之前其他 CPU 的写入**

先记住：**io_uring 的无锁设计依赖内存屏障，否则会读到脏数据**。具体怎么用 liburing 会帮你封装好。

---

## 7. 进阶特性（让你理解 io_uring 为什么强）

### 7.1 链表化 SQE（Linked SQEs）

默认每个 SQE 独立完成。**Linked SQEs** 让你可以把多个 SQE 串成一条链：

```
SQE 1（read）  --链接-->  SQE 2（write）
```

内核**原子地**执行这一组——要么全做完，要么全不做。典型场景：

- 文件拷贝：先 read 到临时缓冲区，再 write 出去
- 协议处理：先 read 头，再 read body，再 write 响应

### 7.2 固定文件 / 固定缓冲区

普通 `read(fd, buf, len)`：内核每次都要查 fd 表、可能要刷新 TLB。

io_uring 提供 **fixed files** 和 **fixed buffers**：

- 提前把 fd 注册到 io_uring 内部表里 → SQE 里直接用"index"代替 fd
- 提前把缓冲区注册到 io_uring 内部 → SQE 里用 index 代替地址
- 内核可以**预先 map**，运行时省掉查找和映射开销

### 7.3 内核轮询（SQPOLL）

默认模式下，没有 I/O 请求时，内核不做事。要等用户态 `io_uring_enter()` 提交后，内核才开始处理。

**SQPOLL 模式**下，内核会启动一个 worker 线程，**持续轮询** SQ 数组。看到新 SQE 立刻执行。

代价：内核多一个 100% CPU 占用的线程（可以绑定到一个空闲核）。

收益：**完全消除 `io_uring_enter()` 系统调用**——用户态提交 SQE 后**不需要任何系统调用**，内核已经主动在做。

### 7.4 IORING_OP_* 操作码

io_uring 不只是文件 I/O。它支持几十种操作：

| 操作码 | 含义 |
|--------|------|
| `IORING_OP_READV` / `WRITEV` | scatter/gather I/O |
| `IORING_OP_ACCEPT` | 接受连接 |
| `IORING_OP_CONNECT` | 主动连接 |
| `IORING_OP_SEND` / `RECV` | 网络收发 |
| `IORING_OP_POLL_ADD` | 等某个 fd 就绪（类似 epoll_ctl） |
| `IORING_OP_OPENAT` / `CLOSE` | 打开/关闭文件 |
| `IORING_OP_FSYNC` / `FDATASYNC` | 刷盘 |
| `IORING_OP_SPLICE` / `TEECOPY` | 零拷贝（在两个 fd 之间搬数据，不经用户态） |
| `IORING_OP_READ` / `WRITE` | 普通读/写 |

基本上 `read`/`write`/`send`/`recv`/`accept`/`connect`/`open`/`fsync` 都能用 io_uring 提交。

---

## 8. 性能优势从何而来

| 优化点 | 节省了什么 |
|--------|-----------|
| 共享内存 SQ/CQ | **省掉系统调用中参数拷贝**（传统方式每次 read/write 都要把参数从用户栈拷到内核栈） |
| `mmap` 共享内存 | **省掉 read 的数据拷贝**（传统方式：磁盘→页缓存→用户缓冲区，io_uring 可以直接给内核页缓存指针） |
| 一次提交多个 SQE | **省掉多次系统调用**（epoll + 多个 read/write = 多次进内核，io_uring 一次进内核批处理） |
| 链表化 SQE | **省掉内核态的逻辑切换**（read/write 不再独立，预先编排好） |
| 固定文件 / 缓冲区 | **省掉 fd 查找 + TLB 刷新** |
| SQPOLL | **省掉所有提交系统调用** |
| 内核侧批处理 | **省掉锁竞争**（内核可以一次性认领多个请求） |

实测：在 NVMe SSD + 高并发场景下，io_uring 可以达到传统 epoll + read/write **2~5 倍**的 IOPS（每秒 I/O 数）。

---

## 9. 代码实践（最小可运行示例）

用 liburing（C 库）写一个最简示例：把 `hello.txt` 内容读出来打印。

### 9.1 准备

```bash
# Ubuntu / Debian
sudo apt install liburing-dev

# 编译
gcc -o read_file read_file.c -luring
```

### 9.2 完整代码（40 行）

```c
#include <stdio.h>
#include <fcntl.h>
#include <liburing.h>

int main(void) {
    struct io_uring ring;
    char buf[64];

    // 1. 初始化 io_uring（队列深度 = 8）
    if (io_uring_queue_init(8, &ring, 0) < 0) {
        perror("io_uring_queue_init");
        return 1;
    }

    // 2. 打开文件
    int fd = open("hello.txt", O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    // 3. 拿一个空 SQE，填好"读"操作
    struct io_uring_sqe *sqe = io_uring_get_sqe(&ring);
    io_uring_prep_read(sqe, fd, buf, sizeof(buf), 0);  // 偏移 0
    sqe->user_data = 0x1234;                            // 打个标签
    io_uring_submit(&ring);                             // 提交

    // 4. 收割
    struct io_uring_cqe *cqe;
    io_uring_wait_cqe(&ring, &cqe);                     // 等完成
    if (cqe->res < 0) {
        fprintf(stderr, "read 失败: %s\n", strerror(-cqe->res));
    } else {
        buf[cqe->res] = '\0';                           // res = 实际读到的字节数
        printf("读到 %d 字节: %s\n", cqe->res, buf);
    }
    io_uring_cqe_seen(&ring, cqe);                      // 告诉内核"我处理了"

    // 5. 清理
    close(fd);
    io_uring_queue_exit(&ring);
    return 0;
}
```

### 9.3 逐步执行过程

| 步骤 | 内存里发生什么 |
|------|---------------|
| `io_uring_queue_init` | 内核分配 ring，mmap 出 SQ/CQ 共享内存 |
| `io_uring_get_sqe` | 返回 SQ 数组里下一个空闲位置的指针 |
| `io_uring_prep_read` | 填好 SQE 的 opcode/fd/addr/len/off |
| `io_uring_submit` | 更新 SQ.tail，调用一次 `io_uring_enter()` |
| **内核** | 看到新 SQE，从磁盘读数据，把结果写到 CQ |
| `io_uring_wait_cqe` | 看 CQ.tail 有变化没，没有就阻塞 |
| 拿到 CQE | `cqe->user_data == 0x1234`，`cqe->res == 实际字节数` |
| `io_uring_cqe_seen` | 更新 CQ.head，通知内核 |

### 9.4 预期输出

```
$ echo "hello io_uring" > hello.txt
$ ./read_file
读到 14 字节: hello io_uring
```

### 9.5 动手练习

1. **改成写文件**：用 `io_uring_prep_write` 把一个字符串写入 `out.txt`
2. **批处理**：循环提交 5 个读不同文件的 SQE，**只调用一次** `io_uring_submit()`，然后用 `io_uring_wait_cqe` 收割 5 个
3. **混合操作**：提交 1 个 `READ` 和 1 个 `FSYNC`，看 CQE 回来的顺序是否对应 user_data

### 9.6 常见坑

| 症状 | 原因 | 修复 |
|------|------|------|
| 读到的内容是脏数据 / 全 0 | 没等 CQE 就访问 buf | 必须在 `io_uring_wait_cqe` 之后才读 buf |
| 提交报错 `EBUSY` / `EAGAIN` | SQ 满了（in-flight 太多） | 增大 queue depth，或先收割几个 CQE 再提交 |
| 收割时 `cqe->res == -ECANCELED` | 内核取消了请求（多见于 syscall 中断） | 检查请求合法性；如果是中断导致，可重提 |
| `io_uring_queue_init` 返回 `EFAULT` | 内核版本太老（< 5.6 缺 io_uring） | 升级内核 |
| 性能提升不明显 | 每次只提交 1 个 SQE | 关键在批处理：攒一批再提交 |

---

## 10. 与邻近概念的关系

| 概念 | 与 io_uring 的关系 |
|------|------------------|
| **epoll** | 通知机制（哪个 fd 就绪了），仍需要 read/write 系统调用；io_uring 是**完整 I/O 提交接口** |
| **libaio（Linux AIO）** | 老的异步 I/O 接口，**只支持 direct I/O 的 O_DIRECT 文件**，不支持网络；io_uring 是它的现代替代 |
| **POSIX AIO (`aio_read`)** | 用户态线程模拟的"伪异步"，性能差；io_uring 是真正的内核异步 |
| **Windows IOCP** | 思路类似（共享队列 + 内核通知），Windows 专有；io_uring 是 Linux 的等价方案，且 API 更现代 |
| **DPDK / SPDK** | 旁路内核、用户态驱动，极致性能（数百万 IOPS），但要独占硬件、专用驱动；io_uring 仍走内核，适合通用场景 |

---

## 11. 常见误解

| 误解 | 事实 |
|------|------|
| **io_uring 一定比 epoll 快** | 不一定。单次小 I/O 场景差距不大；io_uring 在**批处理 + 高并发**下优势明显 |
| **io_uring 会让应用代码更简单** | 也不一定。复杂应用仍要自己管理 SQ/CQ 状态机；好处是**性能上限更高** |
| **io_uring 完全不需要系统调用** | 默认需要 `io_uring_enter`；启用 SQPOLL 模式后才真正"零系统调用" |
| **io_uring 只支持文件 I/O** | 也支持 socket、accept/connect、open、fsync、poll 等，范围很广 |
| **io_uring 是"魔法"自动并行** | 并行由内核决定。批处理只是把"多个独立 I/O"打包提交，让内核一起做 |

---

## 12. 总结

io_uring 用三个核心设计解决了 Linux 异步 I/O 的痛点：

1. **共享内存（mmap）**：用户态和内核态共享 SQ/CQ 缓冲区，省掉参数和数据拷贝
2. **环形队列 + 无锁协议**：用 head/tail 指针 + 内存屏障实现高效的请求/完成匹配
3. **一次系统调用处理多个请求**：批处理 + 内核轮询，最大化吞吐

**知识要点卡片**：

```
io_uring
├─ 数据结构：SQ（提交队列） + CQ（完成队列） + ring buffer
├─ 通信方式：mmap 共享内存，无锁单生产者单消费者协议
├─ 接口：io_uring_setup / io_uring_enter / io_uring_register
├─ 关键 SQE 字段：opcode / fd / addr / len / off / user_data
├─ 关键 CQE 字段：user_data / res（结果）
├─ 进阶：Linked SQE / 固定文件 / 固定缓冲区 / SQPOLL
└─ 性能优势：减少系统调用、零数据拷贝、批处理、内核侧并行
```

---

## 13. 探索路径

1. **直接延伸 — liburing 高级 API**：学 `io_uring_prep_accept` / `io_uring_prep_send` / `io_uring_prep_splice`，写一个完整的 echo 服务器
2. **横向拓展 — io_uring 在数据库/存储引擎中的应用**：例如 RocksDB、TiKV 用 io_uring 替换 libaio 获得性能提升
3. **理论深挖 — 无锁环形队列的实现**：研究 `kernel/io_uring.c` 里 SQ/CQ 的内存屏障细节，理解为什么单生产者单消费者能避免锁
4. **实践项目 — 用 io_uring 重写你的 epoll 程序**：把现有的网络服务从 epoll 迁移到 io_uring，对比 IOPS 和延迟
5. **开放问题 — io_uring 与 io_uring-cmd 的边界**：Linux 内核仍在扩展 io_uring，未来可能覆盖更多子系统（如网络协议栈 bypass）