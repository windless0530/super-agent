"""verify_skills 测试:结构校验、必需 token、契约交叉校验。"""

import importlib.util
import importlib.machinery
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent

_spec = importlib.util.spec_from_file_location(
    "verify_skills", str(REPO / "tools/verify_skills.py"),
    loader=importlib.machinery.SourceFileLoader(
        "verify_skills", str(REPO / "tools/verify_skills.py")),
)
verify_skills = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_skills)


def _write_skill(root: Path, name: str, body: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


GOOD_REPORT = """---
name: sa-report
description: 完成协议与 sactl report 上报规范
---

# sa-report

收尾调用 sactl report,事件字段齐全,幂等,三轮规则必须遵守。
字段:event_id、taskid、outcome、phase、source_msg_id、doc_version、payload。
event_id 每次生成一次复用;outcome 取 completed 或 escalated。
"""

BAD_NAME = """---
name: wrong-name
description: 名字与目录不一致
---

正文
"""


def test_good_skill_passes(tmp_path):
    _write_skill(tmp_path, "sa-report", GOOD_REPORT)
    errors = verify_skills.check_tree(tmp_path, required={"sa-report": ()})
    assert errors == []


def test_bad_frontmatter_or_name_fails(tmp_path):
    _write_skill(tmp_path, "sa-report", BAD_NAME)
    errors = verify_skills.check_tree(tmp_path, required={"sa-report": ()})
    assert any("name" in e for e in errors)


def test_missing_required_token_fails(tmp_path):
    _write_skill(tmp_path, "sa-report",
                 GOOD_REPORT.replace("sactl report", "上报命令"))
    errors = verify_skills.check_tree(tmp_path, required={"sa-report": ("sactl report",)})
    assert any("sactl report" in e for e in errors)


def test_description_limit(tmp_path):
    _write_skill(tmp_path, "sa-report",
                 GOOD_REPORT.replace("完成协议与 sactl report 上报规范", "长" * 201))
    errors = verify_skills.check_tree(tmp_path, required={"sa-report": ()})
    assert any("description" in e for e in errors)


def test_contract_cross_check_report_fields(tmp_path):
    # sa-report 正文缺一个 REPORT_FIELDS 字段名 → 报错
    body = GOOD_REPORT
    for f in verify_skills.REPORT_FIELDS:
        if f in body and f != "event_id":
            body = body.replace(f, "别的名字")
            break
    _write_skill(tmp_path, "sa-report", body)
    errors = verify_skills.check_tree(tmp_path, required={"sa-report": ()})
    assert any("字段" in e or "REPORT_FIELDS" in e for e in errors)


def test_real_skills_all_pass():
    errors = verify_skills.check_tree(REPO / "skills", required=verify_skills.REQUIRED_TOKENS)
    assert errors == [], f"真实 skills 校验失败: {errors}"
    # Task 13 后:全部 7 个 skill 均已注册且通过
    assert set(verify_skills.REQUIRED_TOKENS) == {
        "sa-router", "sa-report", "sa-design", "sa-review",
        "sa-planner", "sa-executor", "sa-devreview",
    }


def test_role_cards_exist():
    for role in ("router", "design", "reviewer", "planner", "executor", "devreviewer"):
        card = REPO / "bots" / role / "CLAUDE.md"
        assert card.exists(), f"缺角色卡: {card}"
        text = card.read_text(encoding="utf-8")
        assert "禁止" in text and "三轮" in text  # 身份 + 红线齐全


def test_cli_exit_code():
    proc = subprocess.run(
        [sys.executable, str(REPO / "tools/verify_skills.py")],
        capture_output=True, text=True, cwd=REPO,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
