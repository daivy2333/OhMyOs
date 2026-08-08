#!/usr/bin/env python3
"""扫描 docs/weeks/、docs/months/ 和 docs/notes/，自动生成首页索引和导航。"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEEKS_DIR = ROOT / "docs" / "weeks"
MONTHS_DIR = ROOT / "docs" / "months"
NOTES_DIR = ROOT / "docs" / "notes"
INDEX_MD = ROOT / "docs" / "index.md"
MKDOCS_YML = ROOT / "mkdocs.yml"


def rel_link(filepath: Path) -> str:
    rel = filepath.relative_to(ROOT / "docs")
    return str(rel.with_suffix("")).replace("\\", "/")


def doc_path(filepath: Path) -> str:
    return filepath.relative_to(ROOT / "docs").as_posix()


def yaml_string(value: str) -> str:
    """生成可安全嵌入 YAML 的双引号字符串。"""
    return json.dumps(value, ensure_ascii=False)


def report_id(filepath: Path) -> str:
    name = filepath.name
    m = re.search(r"(W\d+)\.md$", name)
    if m:
        return m.group(1)
    m = re.search(r"(M\d+)\.md$", name)
    if m:
        return m.group(1)
    return filepath.stem


def extract_report(filepath: Path) -> dict:
    content = filepath.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = m.group(1).strip() if m else filepath.stem
    return {
        "id": report_id(filepath),
        "title": title,
        "link": rel_link(filepath),
        "name": filepath.name,
    }


def collect_reports(directory: Path) -> list:
    reports = [extract_report(f) for f in sorted(directory.glob("*.md"))]
    reports.sort(key=lambda r: r["id"], reverse=True)
    return reports


def extract_note(filepath: Path) -> dict:
    content = filepath.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = m.group(1).strip() if m else filepath.stem
    m = re.search(r"\*\*标签\*\*[：:]\s*(.+)", content)
    tags = [t.strip() for t in m.group(1).split(",")] if m else []
    return {
        "title": title,
        "tags": tags,
        "link": rel_link(filepath),
        "path": doc_path(filepath),
    }


def collect_notes_in(directory: Path) -> list:
    notes = [extract_note(f) for f in directory.glob("*.md")]
    notes.sort(key=lambda n: (n["title"], n["path"]))
    return notes


def collect_notes() -> dict:
    default_notes = collect_notes_in(NOTES_DIR)
    sections = []

    directories = sorted(
        (path for path in NOTES_DIR.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )
    for directory in directories:
        notes = collect_notes_in(directory)
        if notes:
            sections.append({"name": directory.name, "notes": notes})

    return {"default": default_notes, "sections": sections}


def report_table(reports: list) -> str:
    lines = ["| 编号 | 主题 |", "| --- | --- |"]
    for r in reports:
        lines.append(f"| [{r['id']}]({r['link']}) | {r['title']} |")
    return "\n".join(lines)


def note_table(notes: list) -> str:
    lines = ["| 标题 | 标签 |", "| --- | --- |"]
    for n in notes:
        tags_str = ", ".join(n["tags"])
        lines.append(f"| [{n['title']}]({n['link']}) | {tags_str} |")
    return "\n".join(lines)


def note_index(notes: dict) -> str:
    sections = []
    if notes["default"]:
        sections.append(note_table(notes["default"]))

    for section in notes["sections"]:
        sections.append(f"### {section['name']}\n\n{note_table(section['notes'])}")

    return "\n\n".join(sections)


def patch_section(text: str, marker: str, replacement: str) -> str:
    return re.sub(
        rf"<!-- {marker}_START -->.*<!-- {marker}_END -->",
        f"<!-- {marker}_START -->\n{replacement}\n<!-- {marker}_END -->",
        text,
        flags=re.DOTALL,
    )


def generate_index(weeks: list, months: list, notes: dict) -> str:
    content = INDEX_MD.read_text(encoding="utf-8")
    content = patch_section(content, "WEEKLY_INDEX", report_table(weeks))
    content = patch_section(content, "MONTHLY_INDEX", report_table(months))
    content = patch_section(content, "NOTES_INDEX", note_index(notes))
    return content


def nav_items(items: list, directory: str) -> str:
    lines = []
    for item in items:
        title = yaml_string(item["title"])
        path = yaml_string(f"{directory}/{item['name']}")
        lines.append(f"      - {title}: {path}")
    return "\n".join(lines) if lines else ""


def nav_notes(notes: dict) -> str:
    lines = []

    for note in notes["default"]:
        lines.append(
            f"      - {yaml_string(note['title'])}: {yaml_string(note['path'])}"
        )

    for section in notes["sections"]:
        lines.append(f"      - {yaml_string(section['name'])}:")
        for note in section["notes"]:
            lines.append(
                f"          - {yaml_string(note['title'])}: "
                f"{yaml_string(note['path'])}"
            )

    return "\n".join(lines)


def patch_nav_section(content: str, marker: str, replacement: str) -> str:
    start_key = f"NAV_{marker}_START"
    end_key = f"NAV_{marker}_END"
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


def update_mkdocs_nav(weeks: list, months: list, notes: list) -> None:
    content = MKDOCS_YML.read_text(encoding="utf-8")
    content = patch_nav_section(content, "WEEKS", nav_items(weeks, "weeks"))
    content = patch_nav_section(content, "MONTHS", nav_items(months, "months"))
    content = patch_nav_section(content, "NOTES", nav_notes(notes))
    MKDOCS_YML.write_text(content, encoding="utf-8")


def main():
    weeks = collect_reports(WEEKS_DIR)
    months = collect_reports(MONTHS_DIR)
    notes = collect_notes()

    new_index = generate_index(weeks, months, notes)
    INDEX_MD.write_text(new_index, encoding="utf-8")
    note_count = len(notes["default"]) + sum(
        len(section["notes"]) for section in notes["sections"]
    )
    print(
        f"✓ 生成 index.md：{len(weeks)} 篇周报 + {len(months)} 篇月报 "
        f"+ {note_count} 篇笔记（{len(notes['sections'])} 个分区）"
    )

    update_mkdocs_nav(weeks, months, notes)
    print(f"✓ 更新 mkdocs.yml 导航")


if __name__ == "__main__":
    main()
