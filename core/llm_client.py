"""LLM 客户端：一个薄薄的 OpenAI 兼容层 + 离线 mock provider。

设计要点
--------
* **接口可换**：只依赖 ``/chat/completions`` 这一条契约，换 DeepSeek / OpenAI /
  本地 vLLM 只改 ``config/agents.json`` 的 ``base_url`` 和 ``model``，代码不动。
* **按 Agent 配参**：三个 Agent 的温度/上限分开配，审查Agent 用 0 温度保证结论稳定。
* **离线可跑**：``provider=mock`` 时按 ``mock/`` 下的脚本返回内容，
  没有 API Key 也能把整条流水线跑通、把状态机演示清楚。
* **只依赖 requests**：不引入 openai SDK，少一层版本耦合。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "agents.json"
ENV_FILE = PROJECT_ROOT / ".env"


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、返回格式异常）。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _read_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.is_file():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def load_config(path: Optional[Path] = None, overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """读取配置：文件 → .env → 环境变量 → 命令行覆盖，后者优先。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg: Dict[str, Any] = {}
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    else:
        cfg = {"provider": "mock", "agents": {}, "limits": {"max_rounds": 3}}

    file_env = _read_env_file(ENV_FILE)
    env = {**file_env, **os.environ}

    mapping = {
        "provider": "AGENT_PROVIDER",
        "base_url": "AGENT_BASE_URL",
        "model": "AGENT_MODEL",
        "temperature": "AGENT_TEMPERATURE",
        "max_tokens": "AGENT_MAX_TOKENS",
    }
    for key, env_name in mapping.items():
        if env.get(env_name):
            cfg[key] = env[env_name]

    if overrides:
        for k, v in overrides.items():
            if v is not None and v != "":
                cfg[k] = v

    cfg.setdefault("provider", "mock")
    cfg.setdefault("base_url", "https://api.deepseek.com/v1")
    cfg.setdefault("model", "deepseek-chat")
    cfg.setdefault("temperature", 0.2)
    cfg.setdefault("max_tokens", 4096)
    cfg.setdefault("timeout", 120)
    cfg.setdefault("retries", 2)
    cfg.setdefault("agents", {})
    cfg.setdefault("limits", {"max_rounds": 3})

    key_env = cfg.get("api_key_env", "AGENT_API_KEY")
    cfg["api_key"] = env.get(key_env, "") or cfg.get("api_key", "")
    cfg["_root"] = str(PROJECT_ROOT)
    return cfg


def extract_json(text: str) -> Dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 围栏和前后废话）。"""
    if not text:
        raise LLMError("模型返回为空")
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = fenced + [text]
    decoder = json.JSONDecoder()
    for blob in candidates:
        blob = blob.strip()
        start = blob.find("{")
        while start != -1:
            try:
                obj, _ = decoder.raw_decode(blob[start:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            start = blob.find("{", start + 1)
    raise LLMError(f"无法从输出中解析 JSON：{text[:200]}")


def extract_code(text: str) -> str:
    """从模型输出里抠出 Python 代码（优先带 python 标记的围栏）。"""
    if not text:
        raise LLMError("模型返回为空")
    m = re.search(r"```(?:python|py)\s*\n(.*?)```", text, flags=re.S | re.I)
    if m:
        return m.group(1).rstrip() + "\n"
    m = re.search(r"```\s*\n(.*?)```", text, flags=re.S)
    if m:
        return m.group(1).rstrip() + "\n"
    if "class " in text or "def " in text:
        return text.strip() + "\n"
    raise LLMError(f"输出里找不到代码块：{text[:200]}")


# --------------------------------------------------------------------------- #
# mock provider：脚本化场景
# --------------------------------------------------------------------------- #
class MockProvider:
    """离线演示 provider。

    按「第几次调用」返回 ``mock/`` 下预设的文件，模拟一轮真实的
    编码 → 审查打回 → 修复 → 审查通过 的过程。
    """

    # (agent, 第几次调用) -> mock 文件名
    SCRIPT = {
        ("coder", 1): ("coder_r1.py", "draft"),
        ("reviewer", 1): ("reviewer_r1.json", "review"),
        ("fixer", 1): ("fixer_r1.py", "final"),
        ("reviewer", 2): ("reviewer_r2.json", "review"),
    }

    def __init__(self, root: Path):
        self.root = root
        self.calls: Dict[str, int] = {}

    def complete(self, agent: str, system: str, user: str) -> str:
        count = self.calls.get(agent, 0) + 1
        self.calls[agent] = count
        name, kind = self.SCRIPT.get((agent, count), (None, None))

        if name is None:
            # 脚本用完之后：审查员一律放行，编码/修复返回上一份可用代码
            if agent == "reviewer":
                return json.dumps(
                    {"approved": True, "score": 90, "issues": [],
                     "summary": "脚本场景已结束，按已通过处理。"},
                    ensure_ascii=False,
                )
            name, kind = "fixer_r1.py", "final"

        path = self.root / name
        if not path.is_file():
            raise LLMError(f"mock 脚本文件缺失：{path}")
        body = path.read_text(encoding="utf-8")

        if kind == "review":
            return body
        return f"```python\n{body.rstrip()}\n```\n\n（mock provider 输出，来自 {name}）"


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
@dataclass
class LLMClient:
    config: Dict[str, Any]
    mock_root: Optional[Path] = None
    verbose: bool = False
    _calls: List[Dict[str, Any]] = field(default_factory=list)
    _mock: Optional[MockProvider] = None

    def __post_init__(self) -> None:
        if self.config.get("provider") == "mock":
            self._mock = MockProvider(self.mock_root or (PROJECT_ROOT / "mock"))

    # -- 参数 --------------------------------------------------------------- #
    @property
    def provider(self) -> str:
        return str(self.config.get("provider", "mock")).lower()

    def agent_settings(self, agent: str) -> Dict[str, Any]:
        base = {
            "model": self.config.get("model"),
            "temperature": self.config.get("temperature", 0.2),
            "max_tokens": self.config.get("max_tokens", 4096),
        }
        base.update(self.config.get("agents", {}).get(agent, {}) or {})
        return base

    # -- 调用 --------------------------------------------------------------- #
    def chat(self, agent: str, system: str, user: str, *, json_mode: bool = False) -> str:
        if self.provider == "mock":
            text = self._mock.complete(agent, system, user)  # type: ignore[union-attr]
            self._calls.append({"agent": agent, "provider": "mock", "chars": len(text), "ok": True})
            return text

        settings = self.agent_settings(agent)
        url = str(self.config["base_url"]).rstrip("/") + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": settings["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": settings["temperature"],
            "max_tokens": settings["max_tokens"],
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.config.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        if not self.config.get("api_key"):
            raise LLMError(
                "缺少 API Key：在 .env 里设置 AGENT_API_KEY，或改用 --mock 离线模式"
            )

        retries = int(self.config.get("retries", 2))
        timeout = int(self.config.get("timeout", 120))
        last_err: Optional[Exception] = None
        started = time.time()
        for attempt in range(1, retries + 2):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"]
                    self._calls.append({
                        "agent": agent, "provider": self.provider,
                        "model": settings["model"], "chars": len(text),
                        "latency": round(time.time() - started, 2), "ok": True,
                        "usage": data.get("usage", {}),
                    })
                    return text
                if resp.status_code in (401, 403):
                    raise LLMError(f"鉴权失败({resp.status_code})：检查 AGENT_API_KEY / base_url")
                last_err = LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            except requests.RequestException as exc:
                last_err = exc
            if attempt <= retries:
                time.sleep(1.5 * attempt)
        self._calls.append({"agent": agent, "provider": self.provider, "ok": False})
        raise LLMError(f"调用 {agent} 失败（重试 {retries} 次）：{last_err}")

    def chat_json(self, agent: str, system: str, user: str) -> Dict[str, Any]:
        return extract_json(self.chat(agent, system, user, json_mode=True))

    def chat_code(self, agent: str, system: str, user: str) -> str:
        return extract_code(self.chat(agent, system, user))

    @property
    def call_log(self) -> List[Dict[str, Any]]:
        return list(self._calls)
