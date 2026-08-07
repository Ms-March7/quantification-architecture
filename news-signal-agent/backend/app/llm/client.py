from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx


def llm_json(
    system_prompt: str,
    user_prompt: str,
    config: dict[str, Any] | None = None,
    timeout_seconds: float = 20,
) -> dict[str, Any] | None:
    settings = config or {}
    enabled = settings.get("enabled", "auto")
    if enabled is False or str(enabled).lower() in {"false", "off", "disabled"}:
        return None
    provider = str(os.getenv("LLM_PROVIDER") or settings.get("provider") or "").lower()
    base_url = str(os.getenv("LLM_API_BASE") or settings.get("base_url") or "https://api.openai.com/v1")
    if not provider:
        provider = _provider_name(base_url)
    if provider == "codex_cli":
        model = str(os.getenv("CODEX_CLI_MODEL") or settings.get("model") or "")
    else:
        model = str(os.getenv("LLM_MODEL") or settings.get("model") or "gpt-4o-mini")
    prompt_version = str(settings.get("prompt_version") or "default")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": float(settings.get("temperature", 0.1)),
        "response_format": {"type": "json_object"},
    }
    max_tokens = settings.get("max_tokens")
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    thinking = settings.get("thinking")
    if isinstance(thinking, str) and thinking.lower() in {"enabled", "disabled"}:
        payload["thinking"] = {"type": thinking.lower()}
    elif isinstance(thinking, dict):
        payload["thinking"] = thinking
    reasoning_effort = settings.get("reasoning_effort")
    if reasoning_effort:
        payload["reasoning_effort"] = str(reasoning_effort)
    cache_key = _cache_key(
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        payload=payload,
    )
    runtime = settings.get("_runtime") if isinstance(settings.get("_runtime"), dict) else None
    project_root = _project_root(settings.get("_project_root"))
    cache_enabled = str(settings.get("cache", "on")).lower() not in {"false", "off", "disabled"}
    if cache_enabled and project_root:
        try:
            from app.storage.sqlite import load_llm_cache

            cached = load_llm_cache(project_root, cache_key)
        except Exception:
            cached = None
        if cached:
            if runtime is not None:
                runtime["cache_hits"] = int(runtime.get("cache_hits", 0)) + 1
            return cached

    max_calls = int(settings.get("max_calls_per_run") or 0)
    if runtime is not None and max_calls and int(runtime.get("calls", 0)) >= max_calls:
        runtime["budget_skips"] = int(runtime.get("budget_skips", 0)) + 1
        return None

    if provider == "codex_cli":
        parsed, metadata = _run_codex_cli(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            settings=settings,
            model=model,
            prompt_version=prompt_version,
            timeout_seconds=float(settings.get("timeout_seconds") or timeout_seconds),
        )
    else:
        parsed, metadata = _run_http_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            settings=settings,
            base_url=base_url,
            model=model,
            provider=provider,
            prompt_version=prompt_version,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
    if not parsed:
        return None
    if runtime is not None:
        runtime["calls"] = int(runtime.get("calls", 0)) + 1
        runtime.setdefault("usage", []).append(metadata)
    if cache_enabled and project_root:
        try:
            from app.storage.sqlite import store_llm_cache

            store_llm_cache(
                project_root=project_root,
                cache_key=cache_key,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                response=dict(parsed),
                metadata=metadata,
            )
        except Exception:
            pass
    parsed["_llm_meta"] = metadata
    return parsed


def _parse_json_object(content: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        candidate = _first_balanced_json_object(content)
        if not candidate:
            return None
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _run_http_llm(
    system_prompt: str,
    user_prompt: str,
    settings: dict[str, Any],
    base_url: str,
    model: str,
    provider: str,
    prompt_version: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    api_key = os.getenv(str(settings.get("api_key_env") or "LLM_API_KEY")) or os.getenv(
        "DEEPSEEK_API_KEY"
    )
    if not api_key:
        return None, {}
    endpoint = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            response_payload = response.json()
            content = response_payload["choices"][0]["message"]["content"]
    except Exception:
        return None, {}
    parsed = _parse_json_object(content)
    if not parsed:
        return None, {}
    return parsed, _llm_metadata(
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        usage=response_payload.get("usage") if isinstance(response_payload, dict) else {},
    )


def _run_codex_cli(
    system_prompt: str,
    user_prompt: str,
    settings: dict[str, Any],
    model: str,
    prompt_version: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    command = str(os.getenv("CODEX_CLI_COMMAND") or settings.get("command") or "codex")
    sandbox = str(settings.get("sandbox") or "read-only")
    prompt = _codex_json_prompt(system_prompt, user_prompt)
    with tempfile.TemporaryDirectory(prefix="news-signal-codex-") as tmp_dir:
        output_path = Path(tmp_dir) / "codex_last_message.txt"
        schema_path = Path(tmp_dir) / "schema.json"
        schema_path.write_text(
            json.dumps({"type": "object", "additionalProperties": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        args = [
            command,
            "exec",
            "--sandbox",
            sandbox,
            "-c",
            "approval_policy=\"never\"",
            "-c",
            f"model_reasoning_effort=\"{str(settings.get('reasoning_effort') or 'low')}\"",
            "--cd",
            str(settings.get("_project_root") or Path.cwd()),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--color",
            "never",
        ]
        if model:
            args.extend(["--model", model])
        args.append("-")
        try:
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("NO_COLOR", "1")
            completed = subprocess.run(
                args,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return None, {}
        content = ""
        if output_path.exists():
            content = output_path.read_text(encoding="utf-8", errors="replace")
        if not content:
            content = completed.stdout or completed.stderr
        parsed = _parse_json_object(content)
        if not parsed:
            return None, {}
        return parsed, _llm_metadata(
            provider="codex_cli",
            model=model,
            prompt_version=prompt_version,
            usage={"stdout_chars": len(completed.stdout or ""), "stderr_chars": len(completed.stderr or "")},
            extra={"exit_code": completed.returncode, "sandbox": sandbox},
        )


def _codex_json_prompt(system_prompt: str, user_prompt: str) -> str:
    return f"""
You are a JSON-only financial news structuring component.
Do not edit files. Do not run commands. Do not give investment advice.
Return exactly one JSON object as the final answer. No Markdown. No code fences. No explanation.

你是一个只返回 JSON 的财经新闻结构化判断器。不要改文件，不要运行命令，不要给投资建议。
最终答案只能是一个 JSON object，不要 Markdown，不要代码块，不要解释。

系统要求：
{system_prompt}

用户任务：
{user_prompt}

强制输出：
只输出一个 JSON object，不要 Markdown，不要代码块，不要解释。
"""


def _llm_metadata(
    provider: str,
    model: str,
    prompt_version: str,
    usage: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "cache_hit": False,
        "provider": provider,
        "model": model,
        "prompt_version": prompt_version,
        "usage": usage or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        **(extra or {}),
    }


def _first_balanced_json_object(content: str) -> str | None:
    start = content.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(content)):
            char = content[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = content[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return candidate
        start = content.find("{", start + 1)
    return None


def _cache_key(
    provider: str,
    model: str,
    prompt_version: str,
    system_prompt: str,
    user_prompt: str,
    payload: dict[str, Any],
) -> str:
    raw = json.dumps(
        {
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "temperature": payload.get("temperature"),
            "max_tokens": payload.get("max_tokens"),
            "thinking": payload.get("thinking"),
            "reasoning_effort": payload.get("reasoning_effort"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "llm_" + sha256(raw.encode("utf-8")).hexdigest()


def _provider_name(base_url: str) -> str:
    lowered = base_url.lower()
    if "deepseek" in lowered:
        return "deepseek"
    if "openai" in lowered:
        return "openai"
    return lowered.rstrip("/").rsplit("/", 1)[-1] or "unknown"


def _project_root(value: Any) -> Path | None:
    if not value:
        return None
    return value if isinstance(value, Path) else Path(str(value))
