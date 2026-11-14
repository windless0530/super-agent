#!/usr/bin/env python3
"""角色 Skill 结构验证器。

- 校验 skills/sa-*/SKILL.md:frontmatter 合法、name==目录名、description 非空 ≤200 字。
- REQUIRED_TOKENS:每个 Skill 必须包含的协议要素(子串检查)。
- 契约交叉校验:sa-report 正文必须出现 REPORT_FIELDS 全部字段名与 OUTCOMES 两个值
  (单一事实源 = services/coordinator/report.py,提示词与代码不允许漂移)。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.coordinator.report import OUTCOMES, REPORT_FIELDS  # noqa: E402

DESCRIPTION_LIMIT = 200

# REQUIRED_TOKENS:全部 7 个角色 skill 的协议要素(T13 解锁后终版)
REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "sa-router": ("澄清", "类型注册表", "结构化", "禁止", "Get"),
    "sa-report": ("sactl report", "completed", "escalated", "event_id", "幂等", "三轮"),
    "sa-design": ("认领消息", "进展楼", "方案模板", "@评审", "三轮", "sactl report"),
    "sa-review": ("检查单", "可行性", "风险", "与原任务偏差", "三轮", "sactl report"),
    "sa-planner": ("Spec", "方案文档", "@开发评审", "@Executor", "sactl report"),
    "sa-executor": ("worktree", "自测", "自测报告", "@开发评审", "sactl report"),
    "sa-devreview": ("Spec", "diff", "自动修复", "小修", "大改", "sactl report"),
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict | None:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta


def check_skill(md_path: Path, dir_name: str, tokens: tuple[str, ...]) -> list[str]:
    errors = []
    text = md_path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    if meta is None:
        return [f"{dir_name}: 缺 frontmatter"]
    if meta.get("name") != dir_name:
        errors.append(f"{dir_name}: name({meta.get('name')}) 与目录名不一致")
    desc = meta.get("description", "")
    if not desc:
        errors.append(f"{dir_name}: description 为空")
    elif len(desc) > DESCRIPTION_LIMIT:
        errors.append(f"{dir_name}: description 超过 {DESCRIPTION_LIMIT} 字")
    for token in tokens:
        if token not in text:
            errors.append(f"{dir_name}: 缺必需内容 '{token}'")
    # 契约交叉校验(sa-report ↔ report.py 单一事实源)
    if dir_name == "sa-report":
        for f in REPORT_FIELDS:
            if f not in text:
                errors.append(f"sa-report: 契约漂移,正文字段缺失 {f}(REPORT_FIELDS)")
        for o in OUTCOMES:
            if o not in text:
                errors.append(f"sa-report: 契约漂移,outcome 缺失 {o}(OUTCOMES)")
    return errors


def check_tree(root: Path, required: dict[str, tuple[str, ...]]) -> list[str]:
    errors = []
    for dir_name, tokens in sorted(required.items()):
        md = root / dir_name / "SKILL.md"
        if not md.exists():
            errors.append(f"{dir_name}: SKILL.md 不存在")
            continue
        errors.extend(check_skill(md, dir_name, tokens))
    return errors


def main() -> int:
    root = Path(__file__).resolve().parent.parent / "skills"
    errors = check_tree(root, REQUIRED_TOKENS)
    for dir_name in sorted(REQUIRED_TOKENS):
        status = "OK" if not any(e.startswith(f"{dir_name}:") or e.startswith(f"{dir_name} ")
                                 for e in errors) else "FAIL"
        print(f"[{status}] {dir_name}")
    for e in errors:
        print(f"  - {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
