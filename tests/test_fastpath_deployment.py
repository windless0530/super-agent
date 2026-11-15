"""fastpath 部署模板:unit 可加载、入口正确、示例不含真实凭据。"""

import configparser
import subprocess
from pathlib import Path


ROOT = Path(__file__).parent.parent
SYSTEMD = ROOT / "systemd"


def _env(name):
    values = {}
    for line in (SYSTEMD / name).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def test_im_watcher_unit_syntax_and_entrypoint():
    unit = SYSTEMD / "sa-im-watcher.service"
    proc = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(unit)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    parser = configparser.ConfigParser()
    parser.read(unit)
    service = parser["Service"]
    assert service["ExecStart"] == (
        "/usr/bin/python3 tools/im_watcher.py $SA_IM_WATCHER_ARGS"
    )
    assert service["Restart"] == "always"
    assert service["EnvironmentFile"] == "/home/ubuntu/.config/super-agent/im-watcher.env"


def test_llm_and_watcher_examples_have_placeholders_only():
    sa = _env("sa.env.example")
    assert sa["SA_LLM_BASE_URL"] == "https://api.deepseek.com"
    assert sa["SA_LLM_MODEL"] == "deepseek-flash"
    assert sa["SA_LLM_KEY"].startswith("replace-")
    watcher = _env("im-watcher.env.example")
    assert watcher["SA_IM_CURSOR_PATH"] == "registry/im_watcher_cursor.json"
    assert watcher["SA_IM_WATCHER_ARGS"].startswith(
        "--chat-id replace-with-control-chat-id --bot-id replace-"
    )
    assert watcher["SA_COORDINATOR_URL"] == "http://127.0.0.1:8787"
