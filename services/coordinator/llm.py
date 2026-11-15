"""DeepSeek 直连语义判定:固定 prompt、最小上下文、严格 JSON 契约。"""

import json
import os
import urllib.error
import urllib.request

_DEFAULT_MODEL = "deepseek-flash"
_TASK_PROMPT = """你是 Super-agent 的 Router 语义判定器。只输出一个 JSON 对象，不要 Markdown。
任务类型：RD=研发调研，RS=投研/行业报告，DEV=开发实现。
仅当目标对象、任务类型或验收标准含糊到无法执行时才判定歧义；歧义时一次只问一个最关键问题。
标题是不超过 60 字的一句话。task_type 只能是 RD、RS、DEV。
明确时输出 {"ambiguous":false,"task_type":"RD|RS|DEV","title":"..."}；
歧义时输出 {"ambiguous":true,"clarify_question":"..."}。
"""
_CLARIFICATION_PROMPT = _TASK_PROMPT + """当前输入位于既有任务的澄清 thread。先判断它是否回答/修改原任务：
若与原任务无关则 is_new_task=true，否则 false。无论哪种情况，都对该输入对应的任务给出上述 verdict。
输出必须额外包含布尔字段 "is_new_task"。
"""


class VerdictError(RuntimeError):
    """语义调用不可用或返回不符合契约；不得包含凭据。"""


class DeepSeekVerdict:
    def __init__(self, base_url=None, key=None, model=None, timeout=8.0, opener=None):
        self.base_url = (base_url or os.environ.get("SA_LLM_BASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("SA_LLM_KEY", "")
        self.model = model or os.environ.get("SA_LLM_MODEL", _DEFAULT_MODEL)
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen
        if not self.base_url:
            raise VerdictError("缺少 SA_LLM_BASE_URL")
        if not self.key:
            raise VerdictError("缺少 SA_LLM_KEY")

    def decide(self, *, mode: str, context: str) -> dict:
        if mode not in ("task", "clarification"):
            raise VerdictError(f"未知 verdict mode: {mode}")
        if not context.strip():
            raise VerdictError("context 不能为空")
        prompt = _TASK_PROMPT if mode == "task" else _CLARIFICATION_PROMPT
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": context},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 800,
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
            method="POST",
        )
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read())
            content = raw["choices"][0]["message"]["content"]
            verdict = json.loads(content)
            return self._validate(verdict, mode)
        except VerdictError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            raise VerdictError(f"DeepSeek verdict 请求失败: {type(e).__name__}") from e
        except (ValueError, TypeError, KeyError, IndexError) as e:
            raise VerdictError(f"DeepSeek verdict 返回畸形: {type(e).__name__}") from e

    @staticmethod
    def _validate(verdict, mode):
        if not isinstance(verdict, dict) or not isinstance(verdict.get("ambiguous"), bool):
            raise VerdictError("verdict 缺少布尔 ambiguous")
        if mode == "clarification":
            if not isinstance(verdict.get("is_new_task"), bool):
                raise VerdictError("clarification verdict 缺少布尔 is_new_task")
        result = {"ambiguous": verdict["ambiguous"]}
        if mode == "clarification":
            result["is_new_task"] = verdict["is_new_task"]
        if verdict["ambiguous"]:
            question = str(verdict.get("clarify_question") or "").strip()
            if not question:
                raise VerdictError("歧义 verdict 缺少 clarify_question")
            result["clarify_question"] = question
            return result
        task_type = str(verdict.get("task_type") or "").strip()
        title = str(verdict.get("title") or "").strip()
        if task_type not in ("RD", "RS", "DEV"):
            raise VerdictError("verdict task_type 非法")
        if not title or len(title) > 60:
            raise VerdictError("verdict title 为空或超过 60 字")
        result.update(task_type=task_type, title=title)
        return result
