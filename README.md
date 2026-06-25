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
│   │   └── weekly-2026-WXX.md
│   └── notes/                    # 学习笔记
│       └── 任意文件名.md
└── README.md
```

## 新增周报

在 `docs/weeks/` 下新建文件，命名固定为 `weekly-YYYY-WXX.md`（如 `weekly-2026-W02.md`）：

```markdown
# W02 - 一句话主题

**周期**：2026-06-29 ~ 2026-07-05

正文内容...
```

- 第一行 `#` 标题 → 自动提取到索引表「主题」列
- `**周期**：` 行 → 自动提取到索引表「周期」列
- 文件名中的 `WXX` → 自动提取到索引表「周次」列

push 后索引表和侧栏导航自动更新，无需手动改任何配置。

## 新增学习笔记

在 `docs/notes/` 下新建任意命名的 `.md` 文件：

```markdown
# 笔记标题

**日期**：2026-06-25
**标签**：rust, async, tokio

正文内容...
```

- 第一行 `#` 标题 → 自动提取到索引表「标题」列
- `**日期**：` 行 → 自动提取到索引表「日期」列，按日期降序排列
- `**标签**：` 行（逗号分隔）→ 自动提取到索引表「标签」列

文件名自由命名，如 `rust-async.md`、`调度器源码分析.md`。

## 自动生成原理

`scripts/generate.py` 在每次构建前运行，做的事：

1. 扫描 `docs/weeks/` 下所有 `weekly-*.md`，提取周次、标题、周期
2. 扫描 `docs/notes/` 下所有 `*.md`，提取标题、日期、标签
3. 生成 `docs/index.md` 中的索引表（两个模块各一个表格，含表头和数据行）
4. 更新 `mkdocs.yml` 的 `nav` 导航（两个折叠组）

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
