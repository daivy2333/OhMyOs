#!/usr/bin/env python3
"""扫描 docs/weeks/ 和 docs/notes/，自动生成 index.md 索引表 和 mkdocs.yml 导航。"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "docs" / "weeks"
NOTES_DIR = ROOT / "docs" / "notes"
INDEX_MD = ROOT / "docs" / "index.md"
MKDOCS_YML = ROOT / "mkdocs.yml"


def rel_link(filepath: Path) -> str:
    rel = filepath.relative_to(ROOT / "docs")
    return str(rel.with_suffix("")).replace("\\", "/")


def report_type(filepath: Path) -> str:
    stem = filepath.stem.lower()
    if stem.startswith("monthly"):
        return "月报"
    if stem.startswith("weekly"):
        return "周报"
    return "记录"


def report_id(filepath: Path) -> str:
    name = filepath.name
    m = re.search(r"(W\d+)\.md$", name)
    if m:
        return m.group(1)
    m = re.search(r"monthly-(\d{4})-(\d{2})\.md$", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"(M\d+)\.md$", name)
    if m:
        return m.group(1)
    return filepath.stem


def extract_report(filepath: Path) -> dict:
    content = filepath.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = m.group(1).strip() if m else filepath.stem
    m = re.search(r"\*\*周期\*\*[：:]\s*(.+)", content)
    date_range = m.group(1).strip() if m else ""
    return {
        "type": report_type(filepath),
        "id": report_id(filepath),
        "title": title,
        "date_range": date_range,
        "link": rel_link(filepath),
        "name": filepath.name,
    }


def collect_reports() -> list:
    reports = [extract_report(f) for f in sorted(REPORTS_DIR.glob("*.md"))]
    reports.sort(key=lambda r: (r["date_range"], r["id"]), reverse=True)
    return reports


def extract_note(filepath: Path) -> dict:
    content = filepath.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = m.group(1).strip() if m else filepath.stem
    m = re.search(r"\*\*日期\*\*[：:]\s*(.+)", content)
    date = m.group(1).strip() if m else ""
    m = re.search(r"\*\*标签\*\*[：:]\s*(.+)", content)
    tags = [t.strip() for t in m.group(1).split(",")] if m else []
    return {
        "title": title,
        "date": date,
        "tags": tags,
        "link": rel_link(filepath),
        "name": filepath.name,
    }


def collect_notes() -> list:
    notes = [extract_note(f) for f in sorted(NOTES_DIR.glob("*.md"))]
    notes.sort(key=lambda n: n["date"], reverse=True)
    return notes


def report_table(reports: list) -> str:
    lines = ["| 类型 | 编号 | 周期 | 主题 |", "| --- | --- | --- | --- |"]
    for r in reports:
        lines.append(f"| {r['type']} | [{r['id']}]({r['link']}) | {r['date_range']} | {r['title']} |")
    return "\n".join(lines)


def note_table(notes: list) -> str:
    lines = ["| 标题 | 日期 | 标签 |", "| --- | --- | --- |"]
    for n in notes:
        tags_str = ", ".join(n["tags"])
        lines.append(f"| [{n['title']}]({n['link']}) | {n['date']} | {tags_str} |")
    return "\n".join(lines)


def patch_section(text: str, marker: str, replacement: str) -> str:
    return re.sub(
        rf"<!-- {marker}_START -->.*<!-- {marker}_END -->",
        f"<!-- {marker}_START -->\n{replacement}\n<!-- {marker}_END -->",
        text,
        flags=re.DOTALL,
    )


def generate_index(reports: list, notes: list) -> str:
    content = INDEX_MD.read_text(encoding="utf-8")
    content = patch_section(content, "WEEKLY_INDEX", report_table(reports))
    content = patch_section(content, "NOTES_INDEX", note_table(notes))
    return content


def nav_reports(reports: list) -> str:
    lines = []
    for r in reports:
        lines.append(f"      - {r['title']}: weeks/{r['name']}")
    return "\n".join(lines) if lines else ""


def nav_notes(notes: list) -> str:
    lines = []
    for n in notes:
        lines.append(f"      - {n['title']}: notes/{n['name']}")
    return "\n".join(lines) if lines else ""


def patch_nav_section(content: str, marker: str, replacement: str) -> str:
    start_key = f"NAV_{marker}_START"
    end_key = f"NAV_{marker}_END"
    indent = "      "
    lines = []
    inside = False
    for line in content.split("\n"):
        if inside:
            if end_key in line:
                if replacement:
                    lines.append(replacement)
                lines.append(line)
                inside = False
        elif start_key in line:
            lines.append(line)
            inside = True
        else:
            lines.append(line)
    return "\n".join(lines)


def update_mkdocs_nav(reports: list, notes: list) -> None:
    content = MKDOCS_YML.read_text(encoding="utf-8")
    content = patch_nav_section(content, "WEEKS", nav_reports(reports))
    content = patch_nav_section(content, "NOTES", nav_notes(notes))
    MKDOCS_YML.write_text(content, encoding="utf-8")


def main():
    reports = collect_reports()
    notes = collect_notes()

    new_index = generate_index(reports, notes)
    INDEX_MD.write_text(new_index, encoding="utf-8")
    print(f"✓ 生成 index.md：{len(reports)} 篇报告 + {len(notes)} 篇笔记")

    update_mkdocs_nav(reports, notes)
    print(f"✓ 更新 mkdocs.yml 导航")


if __name__ == "__main__":
    main()
