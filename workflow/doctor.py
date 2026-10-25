"""链路自检（doctor）：一条命令回答「现在能不能真的跑起来」。

检查项按依赖顺序排列，任何一项失败都能直接指出问题在哪：

1. **项目文件**：tasks / config / solutions / tests 是否齐全（exe 被单独拷走时最容易坏）；
2. **运行目录可写**：``runs/`` 能不能写（权限问题、只读盘）；
3. **配置**：provider / model / base_url 是否解析成功；
4. **API Key**：是否绑定（没有就提示 ``/bind``）；
5. **模型连通**：真实发一次最小请求（max_tokens=1），报告状态码与耗时；
6. **pytest**：验收器能否执行（打包环境里走 ``exe --pytest``）。

每项返回 ``{name, ok, detail, hint}``，CLI 用 ``--events`` 输出 JSONL，面板渲染成清单。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from core.paths import app_root

ROOT = app_root()


def _add(checks: List[Dict[str, Any]], name: str, ok: bool, detail: str, hint: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint})


def _check_files(checks: List[Dict[str, Any]]) -> None:
    required = ["tasks", "config", "solutions", "tests", "core", "agents", "workflow"]
    missing = [name for name in required if not (ROOT / name).exists()]
    _add(checks, "项目文件", not missing,
         f"根目录 {ROOT}" if not missing else f"缺少：{', '.join(missing)}",
         "把 exe 放回 dist\\ 下，或设置 WORKFLOW_HOME 指向仓库根" if missing else "")


def _check_writable(checks: List[Dict[str, Any]]) -> None:
    probe = ROOT / "runs" / "_doctor"
    try:
        probe.mkdir(parents=True, exist_ok=True)
        target = probe / "probe.txt"
        target.write_text("ok", encoding="utf-8")
        target.unlink()
        _add(checks, "运行目录可写", True, str(ROOT / "runs"))
    except OSError as exc:
        _add(checks, "运行目录可写", False, f"{ROOT / 'runs'}：{exc}",
             "检查目录权限，或把仓库放到可写位置")


def _check_config(checks: List[Dict[str, Any]], config: Dict[str, Any], live: bool = True) -> None:
    provider = str(config.get("provider", "")).lower()
    model = str(config.get("model", ""))
    base_url = str(config.get("base_url", ""))
    ok = bool(provider and model and base_url)
    detail = f"provider={provider or '-'} model={model or '-'} base_url={base_url or '-'}"
    if live and provider == "mock":
        detail += "（面板运行时用 --live 覆盖为真实模型）"
    _add(checks, "模型配置", ok, detail,
         "检查 config/agents.json 或 .env 的 AGENT_MODEL / AGENT_BASE_URL" if not ok else "")


def _check_key(checks: List[Dict[str, Any]], config: Dict[str, Any]) -> bool:
    key = str(config.get("api_key", "") or "")
    if key:
        _add(checks, "API Key", True, f"已绑定（{key[:4]}…{key[-4:]}，共 {len(key)} 位）")
        return True
    _add(checks, "API Key", False, "没有找到 AGENT_API_KEY",
         "在面板里输入 /bind sk-xxxxxxxx 绑定")
    return False


def _check_model(checks: List[Dict[str, Any]], config: Dict[str, Any], has_key: bool) -> None:
    if not has_key:
        _add(checks, "模型连通", False, "跳过：没有 API Key", "先 /bind 绑定 Key")
        return
    try:
        import requests
    except Exception as exc:  # noqa: BLE001
        _add(checks, "模型连通", False, f"requests 不可用：{exc}")
        return

    url = str(config.get("base_url", "")).rstrip("/") + "/chat/completions"
    payload = {
        "model": config.get("model"),
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    started = time.time()
    try:
        resp = requests.post(
            url, json=payload, timeout=20,
            headers={"Authorization": f"Bearer {config.get('api_key')}",
                     "Content-Type": "application/json"},
        )
        elapsed = round(time.time() - started, 2)
        if resp.status_code == 200:
            _add(checks, "模型连通", True, f"HTTP 200（{elapsed}s）· {config.get('model')}")
        elif resp.status_code in (401, 403):
            _add(checks, "模型连通", False, f"HTTP {resp.status_code} 鉴权失败（{elapsed}s）",
                 "Key 可能无效/过期：重新 /bind；或 /unbind 后重新绑定")
        else:
            _add(checks, "模型连通", False, f"HTTP {resp.status_code}（{elapsed}s）：{resp.text[:160]}",
                 "检查 base_url / 模型名 / 账户余额")
    except Exception as exc:  # noqa: BLE001 - 网络异常统一报在这里
        _add(checks, "模型连通", False, f"{type(exc).__name__}: {exc}",
             "检查网络、代理，或 base_url 是否可访问")


def _check_pytest(checks: List[Dict[str, Any]]) -> None:
    try:
        import pytest
        _add(checks, "验收器 pytest", True, f"pytest {pytest.__version__}")
    except Exception as exc:  # noqa: BLE001
        _add(checks, "验收器 pytest", False, f"import pytest 失败：{exc}",
             "pip install -r requirements.txt；打包版请重新运行 scripts/build_exe.ps1")


def _check_events(checks: List[Dict[str, Any]]) -> None:
    try:
        json.dumps({"type": "check", "ok": True}, ensure_ascii=False)
        _add(checks, "事件流序列化", True, "JSONL 可正常序列化（面板依赖）")
    except Exception as exc:  # noqa: BLE001
        _add(checks, "事件流序列化", False, str(exc))


def run_checks(live: bool = True) -> List[Dict[str, Any]]:
    """跑一遍链路自检，返回检查项列表（不抛异常）。"""
    from core.llm_client import load_config

    checks: List[Dict[str, Any]] = []
    _check_files(checks)
    _check_writable(checks)
    try:
        config = load_config()
        _check_config(checks, config, live)
    except Exception as exc:  # noqa: BLE001
        _add(checks, "模型配置", False, f"读取配置失败：{exc}")
        config = {}

    has_key = _check_key(checks, config) if config else False
    if live:
        _check_model(checks, config, has_key)
    else:
        _add(checks, "模型连通", True, "跳过（--offline）")
    _check_pytest(checks)
    _check_events(checks)
    return checks


def summary(checks: List[Dict[str, Any]]) -> str:
    failed = [c["name"] for c in checks if not c["ok"]]
    return "全部通过" if not failed else "未通过：" + "、".join(failed)
