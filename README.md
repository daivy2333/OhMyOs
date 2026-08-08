# OhMyOs

训练营后的自学记录站，基于 MkDocs Material + GitHub Pages。

- 站点地址：`https://daivy2333.github.io/OhMyOs/`
- 训练营期间记录：[2026sOsReport](https://github.com/daivy2333/2026sOsReport)

## 技术栈

- **MkDocs** + **Material 主题**：从 Markdown 生成静态站点
- **GitHub Actions**：push 后自动 `mkdocs build` 并部署到 Pages
- **`scripts/generate.py`**：构建前自动扫描 `docs/` 目录，生成索引页和导航

## 文件结构

```
OhMyOs/
├── .github/workflows/docs.yml   # CI：push → 扫描 → 构建 → 部署
├── scripts/generate.py           # 自动生成索引表和导航
├── mkdocs.yml                    # MkDocs + Material 主题配置
├── requirements.txt              # Python 依赖
├── docs/
│   ├── index.md                  # 首页（索引表由脚本填充）
│   ├── weeks/                    # 周报
│   │   ├── weekly-2026-WXX.md
│   ├── months/                   # 月报
│   │   └── monthly-YYYY-MXX.md
│   └── notes/                    # 学习笔记
│       ├── 任意文件名.md          # 默认分区
│       └── 分区名/                # 一级目录自动生成同名分区
│           └── 任意文件名.md
└── README.md
```

## 新增周报

在 `docs/weeks/` 下新建 `.md` 文件。周报建议命名为 `weekly-YYYY-WXX.md`（如 `weekly-2026-W02.md`）。

```markdown
# W02 - 一句话主题

正文内容...
```

- 第一行 `#` 标题 → 自动提取到索引表「主题」列
- 文件名中的 `WXX` → 自动提取到索引表「编号」列

## 新增月报

在 `docs/months/` 下新建 `.md` 文件。月报建议命名为 `monthly-YYYY-MXX.md`（如 `monthly-2026-M00.md`）。

```markdown
# M00 - 一句话主题

正文内容...
```

- 第一行 `#` 标题 → 自动提取到索引表「主题」列
- 文件名中的 `MXX` → 自动提取到索引表「编号」列；年份只保留在文件名里，不参与编号和排序

push 后索引表和侧栏导航自动更新，无需手动改任何配置。

## 新增学习笔记

笔记可以直接放在 `docs/notes/` 下，也可以放进一级子目录：

```text
docs/notes/
├── async-await-runtime.md
├── 异步驱动/
│   └── async-uart-driver-architecture.md
└── 硬件基础/
    └── mmio-intro.md
```

- `docs/notes/*.md` 属于默认分区，在首页和侧栏中直接展示。
- `docs/notes/<分区名>/*.md` 按一级目录分组，目录名就是网页上的分区名。
- 只扫描一级分区，不扫描更深目录中的笔记。
- 每篇笔记只有一个主分区；需要表达其他分类时继续使用标签。

笔记内容保持现有格式：

```markdown
# 笔记标题

**标签**：rust, async, tokio

正文内容...
```

- 第一行 `#` 标题 → 自动提取到索引表「标题」列
- `**标签**：` 行（逗号分隔）→ 自动提取到索引表「标签」列

文件名和分区名可以自由命名。移动已有笔记后，需同步更新仓库内指向它的相对链接；站点不会为旧地址生成重定向。

## 自动生成原理

`scripts/generate.py` 在每次构建前运行，做的事：

1. 扫描 `docs/weeks/` 下所有 `*.md`，提取编号、标题
2. 扫描 `docs/months/` 下所有 `*.md`，提取编号、标题
3. 扫描 `docs/notes/*.md` 和 `docs/notes/<分区名>/*.md`，提取标题、标签和分区
4. 生成 `docs/index.md` 中的周报、月报和分区笔记索引
5. 更新 `mkdocs.yml` 的 `nav` 导航；默认笔记直接展示，分区笔记生成折叠组

脚本是幂等的——每次全量重建，执行多少遍结果都一样，不会重复追加。

## 本地预览（可选）

```bash
pip install -r requirements.txt
python scripts/generate.py
mkdocs serve
```

浏览器打开 `http://127.0.0.1:8000/OhMyOs/` 预览。

## 部署

push 到 `main` 分支后，GitHub Actions 自动执行：

```
checkout → pip install → generate.py → mkdocs build → deploy to Pages
```

站点约 30 秒后更新。不需要本地安装 Ruby、Jekyll 或任何其他工具。
