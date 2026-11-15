#!/usr/bin/env python3
"""生成 per-bot 会话级模型凭据(bots/<role>/.claude/settings.json)。

从 ~/.config/ai/<provider>.json 读 env 块,写到各角色目录的 project settings;
文件含 ANTHROPIC_AUTH_TOKEN,已在 .gitignore(bots/*/.claude/settings.json)。
用法: python3 scripts/gen_bot_settings.py [--model-override role=model]
"""

import argparse
import json
import os
import stat
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_DIR = os.path.expanduser("~/.config/ai")

# 角色 → (provider 配置文件, 模型覆盖或 None)
ROLE_PROVIDER = {
    "design": ("glm.json", None),
    "planner": ("glm.json", None),
    "executor": ("glm.json", None),
    "reviewer": ("kimi.json", None),
    "devreviewer": ("kimi.json", None),
    "router": ("deepseek.json", "deepseek-v4.1-flash"),  # spec 定 flash;不可用则回退 v4-pro(Task 5 实测)
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-override", action="append", default=[],
                    help="role=model 覆盖,可重复")
    args = ap.parse_args()
    overrides = dict(o.split("=", 1) for o in args.model_override)

    for role, (provider_file, model_override) in ROLE_PROVIDER.items():
        src = os.path.join(AI_DIR, provider_file)
        with open(src, encoding="utf-8") as f:
            cfg = json.load(f)
        env = dict(cfg.get("env") or {})
        if not env.get("ANTHROPIC_BASE_URL") or not env.get("ANTHROPIC_AUTH_TOKEN"):
            print(f"skip {role}: {provider_file} 缺 env 块", file=sys.stderr)
            continue
        model = overrides.get(role) or model_override or env.get("ANTHROPIC_MODEL")
        if model:
            env["ANTHROPIC_MODEL"] = model
        out_dir = os.path.join(REPO, "bots", role, ".claude")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, "settings.json")
        payload = {"env": env}
        if model:
            payload["model"] = model
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)  # 600
        base = env["ANTHROPIC_BASE_URL"]
        masked = env["ANTHROPIC_AUTH_TOKEN"][:6] + "..."
        print(f"{role}: {out} model={model} base={base} token={masked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
