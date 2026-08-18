"""邮件投递：smtplib 发送 HTML 正文 + 附件；dry_run 模式生成 .eml 供人工检查。

设计依据（调研结论）：edu 反垃圾网关可能拦截 .html 附件 →
默认「正文内嵌完整 HTML 日报」+ 附件仅 trace 文本；.html 附件由配置开关控制。
"""
from __future__ import annotations

import smtplib
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

from .tracelog import TraceLog


class MailerError(Exception):
    pass


def send_daily_report(
    smtp_cfg: dict,
    subject: str,
    html_body: str,
    attachments: list[tuple[str, bytes, str]],
    dry_run: bool,
    eml_dir: Path | None = None,
    trace: TraceLog | None = None,
) -> dict:
    """发送日报邮件。

    attachments: [(文件名, bytes, mime_type)]，如 [("agent-trace-2026-08-18.txt", b"...", "text/plain")]
    返回: {"sent": bool, "detail": str}
    """
    user = (smtp_cfg.get("user") or "").strip()
    password = (smtp_cfg.get("password") or "").strip()
    to = [x for x in (smtp_cfg.get("to") or []) if x]

    if not to:
        raise MailerError("未配置收件人 smtp.to")
    if not dry_run and (not user or not password):
        raise MailerError("未配置发件账号 smtp.user / smtp.password（需 SMTP 授权码）")

    from_name = smtp_cfg.get("from_name") or "无人机感知与反制技术日报"
    from_addr = user or "no-reply@example.com"
    msg = MIMEMultipart()
    msg["From"] = formataddr((str(Header(from_name, "utf-8")), from_addr))
    msg["To"] = ", ".join(to)
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg["X-Agent-Trace"] = "generated-by-drone-security-daily-agent"

    body = MIMEText(html_body, "html", "utf-8")
    body.add_header("Content-Disposition", "inline")
    msg.attach(body)

    for fname, content, mime in attachments:
        if mime.startswith("text/"):
            part = MIMEText(content.decode("utf-8"), mime.split("/")[1], "utf-8")
        else:
            part = MIMEApplication(content, _subtype=mime.split("/")[-1])
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", fname))
        msg.attach(part)

    if dry_run:
        eml_dir = eml_dir or Path("data/emails")
        eml_dir.mkdir(parents=True, exist_ok=True)
        ts = formatdate(localtime=True).replace(" ", "_").replace(":", "-")
        path = eml_dir / f"{subject[:40]}_{ts}.eml"
        path.write_bytes(msg.as_bytes())
        detail = f"dry-run：已生成 {path}（未真实发送）"
        if trace:
            trace.step(stage="email", action="tool_call", tool="send_email",
                       input_summary=f"dry_run to={to}", output_summary=detail)
        return {"sent": False, "detail": detail}

    try:
        with smtplib.SMTP_SSL(smtp_cfg.get("host"), int(smtp_cfg.get("port", 465)), timeout=60) as server:
            server.login(user, password)
            server.send_message(msg)
    except Exception as e:  # noqa: BLE001
        if trace:
            trace.step(stage="email", action="tool_call", tool="send_email",
                       input_summary=f"to={to}", output_summary="发送失败",
                       status="error", error={"type": type(e).__name__, "msg": str(e)[:300]})
        raise MailerError(f"邮件发送失败: {e}") from e
    detail = f"已发送至 {', '.join(to)}（正文含日报，附件 {len(attachments)} 个）"
    if trace:
        trace.step(stage="email", action="tool_call", tool="send_email",
                   input_summary=f"to={to}", output_summary=detail)
    return {"sent": True, "detail": detail}
