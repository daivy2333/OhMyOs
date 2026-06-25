# OhMyOs

按周记录学习进度的 GitHub Pages 站点。

- 主页：[`index.md`](index.md)（Jekyll 自动渲染为站点首页）
- 周报目录：[`weeks/`](weeks/)
- 训练营期间的学习记录见 [2026sOsReport](https://github.com/daivy2333/2026sOsReport)

## 启用 Pages

仓库 Settings → Pages → Source 选 `main` 分支根目录即可。`minima` 主题会由 GitHub 自动构建。

默认域名：`https://<username>.github.io/OhMyOs/`

## 新增一周

1. 在 `weeks/` 下新建 `weekly-2026-WXX.md`，复制现有周报的 front matter 模板
2. 在 [`index.md`](index.md) 的"周报索引"表格里补一行
3. 提交并 push，`main` 分支更新后 Pages 会在 1 分钟内重建

## front matter 模板

```yaml
---
title: "WXX - <一句话主题>"
date: YYYY-MM-DD
---
```

页面 URL 形如：`https://<username>.github.io/OhMyOs/weeks/weekly-2026-WXX/`

（Jekyll 默认对带 front matter 的页面启用 pretty permalink，浏览器访问时是否带尾斜杠都能命中）

## 文件结构

```
.
├── _config.yml             # Jekyll 配置（minima 主题）
├── index.md                # 索引首页
├── weeks/                  # 周报目录
│   └── weekly-2026-WXX.md
└── README.md
```
