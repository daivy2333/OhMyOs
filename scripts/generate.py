#!/usr/bin/env python3
"""扫描 docs/weeks/ 下所有周报，自动生成 index.md 索引表 和 mkdocs.yml 导航。"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEEKS_DIR = ROOT / "docs" / "weeks"
INDEX_MD = ROOT / "docs" / "index.md"
MKDOCS_YML = ROOT / "mkdocs.yml"


def extract_week_info(filepath: Path) -> dict:
    """从周报文件提取：周次、标题、日期范围、链接。"""
    content = filepath.read_text(encoding="utf-8")

    # 文件名提取周次：weekly-2026-W01.md → W01
    m = re.search(r"(W\d+)\.md$", filepath.name)
    week_num = m.group(1) if m else "??"

    # 第一个一级标题
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = m.group(1).strip() if m else week_num

    # 周期行
    m = re.search(r"\*\*周期\*\*[：:]\s*(.+)", content)
    date_range = m.group(1).strip() if m else ""

    # MkDocs 内部链接（相对于 docs/）
    rel = filepath.relative_to(ROOT / "docs")
    link = str(rel.with_suffix("")).replace("\\", "/")

    return {
        "week": week_num,
        "title": title,
        "date_range": date_range,
        "link": link,
    }


def collect_weeks() -> list[dict]:
    weeks = []
    for f in sorted(WEEKS_DIR.glob("weekly-*.md")):
        weeks.append(extract_week_info(f))
    # 按周次降序（最新的在前）
    weeks.sort(key=lambda w: w["week"], reverse=True)
    return weeks


def generate_index(weeks: list[dict]) -> str:
    template = INDEX_MD.read_text(encoding="utf-8")

    lines = ["| 周次 | 周期 | 主题 |", "| --- | --- | --- |"]
    for w in weeks:
        lines.append(
            f"| [{w['week']}]({w['link']}) | {w['date_range']} | {w['title']} |"
        )

    table = "\n".join(lines)

    result = re.sub(
        r"<!-- WEEKLY_INDEX_START -->.*<!-- WEEKLY_INDEX_END -->",
        f"<!-- WEEKLY_INDEX_START -->\n{table}\n<!-- WEEKLY_INDEX_END -->",
        template,
        flags=re.DOTALL,
    )
    return result


def generate_nav_section(weeks: list[dict]) -> str:
    """生成 mkdocs.yml 中 nav 的周报部分。"""
    if not weeks:
        return "      - 暂无周报"

    lines = []
    for w in weeks:
        lines.append(f"      - {w['title']}: weeks/{Path(w['link']).name}.md")
    return "\n".join(lines)


def update_mkdocs_nav(weeks: list[dict]) -> None:
    """更新 mkdocs.yml 中 NAV_WEEKS 标记之间的导航内容。"""
    content = MKDOCS_YML.read_text(encoding="utf-8")
    new_section = generate_nav_section(weeks)

    result = re.sub(
        r"(#[^\n]*NAV_WEEKS_START[^\n]*\n).*?(\n[^\n]*#[^\n]*NAV_WEEKS_END)",
        rf"\1{new_section}\2",
        content,
        flags=re.DOTALL,
    )
    MKDOCS_YML.write_text(result, encoding="utf-8")


def main():
    weeks = collect_weeks()

    # 生成 index.md
    new_index = generate_index(weeks)
    INDEX_MD.write_text(new_index, encoding="utf-8")
    print(f"✓ 生成 index.md：{len(weeks)} 篇周报")

    # 更新 mkdocs.yml nav
    update_mkdocs_nav(weeks)
    print(f"✓ 更新 mkdocs.yml 导航")


if __name__ == "__main__":
    main()
