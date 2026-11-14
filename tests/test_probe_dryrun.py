"""探针 dry-run 测试:命令渲染、占位符替换、配置校验。"""

import importlib.util
import importlib.machinery
import json
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "probe_comment_event", str(REPO / "tools/probe/probe_comment_event.py"),
    loader=importlib.machinery.SourceFileLoader(
        "probe_comment_event", str(REPO / "tools/probe/probe_comment_event.py")),
)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

CONFIG = REPO / "tools/probe/probe_config.json"


def test_config_has_five_steps():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    names = [s["name"] for s in cfg["steps"]]
    assert names == ["create_doc", "subscribe", "first_comment",
                     "second_reply", "listen"]


def test_dry_run_renders_all_steps_no_placeholder_left(capsys):
    code = probe.main(["--config", str(CONFIG), "--dry-run",
                       "--doc-token", "docx_TEST", "--out", "/tmp/probe-out.md"])
    assert code == 0
    out = capsys.readouterr().out
    step_lines = [l for l in out.splitlines() if l.startswith("[dry-run] [")]
    assert len(step_lines) == 5
    cmd_lines = [l for l in out.splitlines() if l.strip().startswith("$ ")]
    # 已知上下文占位符必须全部替换(捕获型占位符允许保留,live 由前序步骤回填)
    for line in cmd_lines:
        assert "{doc_token}" not in line
        assert "{listen_seconds}" not in line
        assert "{out_dir}" not in line
    for kw in ("create_doc", "subscribe", "first_comment",
               "second_reply", "listen"):
        assert kw in out


def test_dry_run_substitutes_doc_token(capsys):
    probe.main(["--config", str(CONFIG), "--dry-run",
                "--doc-token", "docx_ABC123", "--out", "/tmp/probe-out.md"])
    out = capsys.readouterr().out
    assert "docx_ABC123" in out


def test_missing_step_field_reports_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"steps": [{"name": "only_name"}]}), encoding="utf-8")
    code = probe.main(["--config", str(bad), "--dry-run", "--out", "/tmp/x.md"])
    assert code == 2
    assert "argv" in capsys.readouterr().err


def test_assertions_rendered_in_report(capsys):
    probe.main(["--config", str(CONFIG), "--dry-run",
                "--doc-token", "docx_X", "--out", "/tmp/probe-out2.md"])
    out = capsys.readouterr().out
    assert "comment_add_v1" in out  # 订阅事件类型出现在渲染中
    assert "断言" in out or "assert" in out.lower()
