from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from app.config import load_notify_config
from app.models import SignalItem
from app.storage.sqlite import notification_allowed, record_notification


def dispatch_notifications(
    project_root: Path,
    signals: list[SignalItem],
    retained_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    _ = retained_events
    config = load_notify_config(project_root)
    notify_config = config.get("notify", {})
    if not notify_config.get("enabled", False):
        return []

    threshold = _threshold(config)
    default_channels = notify_config.get("default_channels", ["console"])
    cooldown_minutes = int(notify_config.get("cooldown_minutes", 120))
    daily_limit_per_symbol = int(notify_config.get("daily_limit_per_symbol", 3))
    candidates = [signal for signal in signals if abs(signal.final_score) >= threshold]
    messages: list[dict[str, Any]] = []
    for signal in candidates:
        text = _message_text(signal)
        for channel_name in default_channels:
            channel_config = config.get("channels", {}).get(channel_name, {})
            if channel_name != "console" and not channel_config.get("enabled", False):
                continue
            notification_key = _notification_key(signal)
            allowed, reason = notification_allowed(
                project_root=project_root,
                notification_key=notification_key,
                channel=channel_name,
                symbol=signal.symbol,
                cooldown_minutes=cooldown_minutes,
                daily_limit_per_symbol=daily_limit_per_symbol,
            )
            if allowed:
                result = _send(channel_name, channel_config, text)
            else:
                result = {"status": "skipped", "detail": reason}
            notification_id = _notification_id(signal, channel_name)
            record_notification(
                project_root=project_root,
                notification_id=notification_id,
                notification_key=notification_key,
                channel=channel_name,
                symbol=signal.symbol,
                score=signal.final_score,
                status=result["status"],
                detail=result["detail"],
            )
            messages.append(
                {
                    "id": notification_id,
                    "channel": channel_name,
                    "symbol": signal.symbol,
                    "score": signal.final_score,
                    "status": result["status"],
                    "detail": result["detail"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    return messages


def _threshold(config: dict[str, Any]) -> float:
    rules = config.get("rules", [])
    for rule in rules:
        if rule.get("name") != "high_score_signal":
            continue
        text = str(rule.get("when") or "")
        for token in text.replace(")", " ").split(">="):
            token = token.strip()
            if token.isdigit():
                return float(token)
    return 80


def _send(channel_name: str, channel_config: dict[str, Any], text: str) -> dict[str, str]:
    if channel_name == "console":
        print(text)
        return {"status": "sent", "detail": "console"}
    if channel_name == "wecom":
        return _send_wecom(channel_config, text)
    if channel_name == "pushplus":
        return _send_pushplus(channel_config, text)
    if channel_name == "serverchan":
        return _send_serverchan(channel_config, text)
    if channel_name == "email":
        return _send_email(channel_config, text)
    if channel_name == "sms_webhook":
        return _send_sms_webhook(channel_config, text)
    return {"status": "skipped", "detail": f"unsupported channel {channel_name}"}


def _send_wecom(channel_config: dict[str, Any], text: str) -> dict[str, str]:
    webhook = os.getenv(str(channel_config.get("webhook_env") or ""))
    if not webhook:
        return {"status": "skipped", "detail": "missing wecom webhook env"}
    with httpx.Client(timeout=8) as client:
        response = client.post(webhook, json={"msgtype": "text", "text": {"content": text}})
    return {"status": "sent" if response.is_success else "failed", "detail": str(response.status_code)}


def _send_pushplus(channel_config: dict[str, Any], text: str) -> dict[str, str]:
    token = os.getenv(str(channel_config.get("token_env") or ""))
    if not token:
        return {"status": "skipped", "detail": "missing pushplus token env"}
    with httpx.Client(timeout=8) as client:
        response = client.post(
            "https://www.pushplus.plus/send",
            json={"token": token, "title": "News Signal Agent", "content": text},
        )
    return {"status": "sent" if response.is_success else "failed", "detail": str(response.status_code)}


def _send_serverchan(channel_config: dict[str, Any], text: str) -> dict[str, str]:
    sendkey = os.getenv(str(channel_config.get("sendkey_env") or ""))
    if not sendkey:
        return {"status": "skipped", "detail": "missing serverchan sendkey env"}
    with httpx.Client(timeout=8) as client:
        response = client.post(
            f"https://sctapi.ftqq.com/{sendkey}.send",
            data={"title": "News Signal Agent", "desp": text},
        )
    return {"status": "sent" if response.is_success else "failed", "detail": str(response.status_code)}


def _send_email(channel_config: dict[str, Any], text: str) -> dict[str, str]:
    host = os.getenv(str(channel_config.get("smtp_host_env") or ""))
    port = int(os.getenv(str(channel_config.get("smtp_port_env") or ""), "587"))
    username = os.getenv(str(channel_config.get("username_env") or ""))
    password = os.getenv(str(channel_config.get("password_env") or ""))
    to_addr = os.getenv(str(channel_config.get("to_env") or ""))
    if not host or not username or not password or not to_addr:
        return {"status": "skipped", "detail": "missing email env"}
    message = MIMEText(text, "plain", "utf-8")
    message["Subject"] = "News Signal Agent"
    message["From"] = username
    message["To"] = to_addr
    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, [to_addr], message.as_string())
    except Exception as exc:
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    return {"status": "sent", "detail": "email"}


def _send_sms_webhook(channel_config: dict[str, Any], text: str) -> dict[str, str]:
    webhook = os.getenv(str(channel_config.get("webhook_env") or ""))
    phone = os.getenv(str(channel_config.get("phone_env") or ""))
    if not webhook:
        return {"status": "skipped", "detail": "missing sms webhook env"}
    payload = {"text": text, "phone": phone}
    headers = {}
    token = os.getenv(str(channel_config.get("token_env") or ""))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=8) as client:
            response = client.post(webhook, json=payload, headers=headers)
    except Exception as exc:
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    return {"status": "sent" if response.is_success else "failed", "detail": str(response.status_code)}


def _message_text(signal: SignalItem) -> str:
    direction = "利好" if signal.final_score > 0 else "利空"
    return (
        f"{direction} {signal.symbol} {signal.name or ''}\n"
        f"分数: {signal.final_score:.2f}\n"
        f"周期: {signal.primary_horizon.value}\n"
        f"判断: {signal.brief}"
    )


def _notification_id(signal: SignalItem, channel_name: str) -> str:
    raw = f"{signal.id}:{channel_name}:{datetime.now(timezone.utc).date().isoformat()}"
    return "notify_" + sha256(raw.encode("utf-8")).hexdigest()[:16]


def _notification_key(signal: SignalItem) -> str:
    raw = f"{signal.symbol}:{signal.event_id}:{signal.direction.value}"
    return "notify_key_" + sha256(raw.encode("utf-8")).hexdigest()[:16]
