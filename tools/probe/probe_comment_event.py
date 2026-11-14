#!/usr/bin/env python3
"""P0 探针 #1:文档评论消息接口(drive.notice.comment_add_v1)可用性验证。

五步编排:建测试文档 → 订阅事件 → 首评 → 二次回复 → 监听捕获。
- 命令全部来自 probe_config.json 的 steps[].argv 模板(现场可校准,不改代码)。
- --dry-run 只渲染打印命令与断言计划,零网络;正式跑需真实飞书环境。
- 结论模板:available(事件为主+低频对账)/ unavailable(轮询降级)。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REQUIRED_STEP_FIELDS = ("name", "desc", "argv")


def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    for step in cfg.get("steps", []):
        missing = [f for f in REQUIRED_STEP_FIELDS if f not in step]
        if missing:
            raise ValueError(
                f"配置错误: step {step.get('name', '?')} 缺字段 {missing}"
            )
    return cfg


def render(argv: list[str], ctx: dict) -> list[str]:
    out = []
    for a in argv:
        for k, v in ctx.items():
            a = a.replace("{" + k + "}", str(v))
        out.append(a)
    return out


def has_placeholder(text: str) -> bool:
    return "{" in text and "}" in text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).parent / "probe_config.json"))
    parser.add_argument("--dry-run", action="store_true", help="只渲染命令,不执行")
    parser.add_argument("--doc-token", default="docx_TEST", help="跳过建文档步骤时直接指定")
    parser.add_argument("--listen-seconds", type=int, default=120)
    parser.add_argument("--out", default="docs/p0/2026-11-14-probe1-评论事件.md")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"配置文件不存在: {e}", file=sys.stderr)
        return 2
    ctx = {
        "doc_token": args.doc_token,
        "listen_seconds": args.listen_seconds,
        "out_dir": str(Path(args.out).parent),
    }
    lines = ["# P0 探针 #1:评论事件接口验证记录", ""]
    lines.append(f"- 配置: {args.config}")
    lines.append(f"- 模式: {'dry-run' if args.dry_run else 'live'}")
    lines.append("")

    for step in cfg["steps"]:
        argv_rendered = render(step["argv"], ctx)
        unresolved = [a for a in argv_rendered if has_placeholder(a)]
        desc = f"[{step['name']}] {step['desc']}"
        if args.dry_run:
            print(f"[dry-run] {desc}")
            print("  $ " + " ".join(argv_rendered))
            if step.get("assert"):
                print(f"  断言: {step['assert']}")
            lines += [f"## {step['name']}", "", f"- 命令: `{' '.join(argv_rendered)}`", ""]
            if step.get("assert"):
                lines += [f"- 断言: {step['assert']}"]
            if unresolved:
                print(f"  注意: 未替换占位符 {unresolved} (live 模式由前序步骤捕获回填)")
            continue
        # live 模式:执行并回填捕获
        proc = subprocess.run(argv_rendered, capture_output=True, text=True, timeout=120)
        lines += [f"## {step['name']}", "",
                  f"- 命令: `{' '.join(argv_rendered)}`",
                  f"- rc: {proc.returncode}", f"- stdout: {proc.stdout[:2000]}", ""]
        if proc.returncode != 0:
            print(f"步骤 {step['name']} 失败: {proc.stderr[:300]}", file=sys.stderr)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text("\n".join(lines), encoding="utf-8")
            return 1
        cap = step.get("capture")
        if cap:
            import re
            m = re.search(step.get("capture_regex", "(\\S+)"), proc.stdout)
            if m:
                ctx[cap] = m.group(1)

    if args.dry_run:
        tpl = cfg.get("conclusion_template", {})
        print("[dry-run] 结论分支: available / unavailable →")
        for k, v in tpl.items():
            print(f"  - {k}: {v}")
        lines += ["## 结论(模板)", ""]
        for k, v in tpl.items():
            lines.append(f"- **{k}**: {v}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"记录写入: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
