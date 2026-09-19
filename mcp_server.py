#!/usr/bin/env python3
"""
claude-mail-bridge MCP Server
给你的 AI 一个邮箱——MCP 版。
AI 可以主动收信、发信、搜索邮件。
发信通过 IMAP 存草稿（绕过 Railway 等云平台的 SMTP 端口封锁）。

Author: Claude Opus 4.6 & its human
License: MIT
"""

import os
import json
import imaplib
import email
import logging
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from email.utils import formataddr, formatdate
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("mail_bridge")

# ── Load Config ────────────────────────────────────────────────

def load_config() -> dict:
    if os.environ.get("MAIL_ADDRESS"):
        return {
            "email": {
                "address": os.environ["MAIL_ADDRESS"],
                "password": os.environ["MAIL_PASSWORD"],
                "imap_host": os.environ.get("IMAP_HOST", "imap.qq.com"),
                "imap_port": int(os.environ.get("IMAP_PORT", "993")),
                "display_name": os.environ.get("DISPLAY_NAME", "Claude"),
            }
        }
    p = Path(__file__).parent / "config.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    raise RuntimeError("请设置环境变量或创建 config.json")

CFG = load_config()
EMAIL = CFG["email"]

# Bark 推送地址（可选）
BARK_URL = os.environ.get("BARK_URL", "")

# ── MCP Server ─────────────────────────────────────────────────

_port = int(os.environ.get("PORT", "8877"))
mcp = FastMCP("mail_bridge", host="0.0.0.0", port=_port)

# ── Helpers ────────────────────────────────────────────────────

def _decode(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _get_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _send_imap_id(conn):
    """163/126 等网易邮箱要求客户端先发 IMAP ID 命令自报身份，
    否则 SELECT 会返回 "Unsafe Login"。"""
    try:
        if "ID" in getattr(conn, "capabilities", ()):
            imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
            conn._simple_command(
                "ID", '("name" "claude-mail-bridge" "version" "1.0" "vendor" "open-source")'
            )
    except Exception:
        pass


def _imap():
    conn = imaplib.IMAP4_SSL(EMAIL["imap_host"], EMAIL.get("imap_port", 993))
    conn.login(EMAIL["address"], EMAIL["password"])
    _send_imap_id(conn)
    return conn


def _summary(msg, uid: str) -> dict:
    return {
        "uid": uid,
        "from": _decode(msg.get("From", "")),
        "to": _decode(msg.get("To", "")),
        "subject": _decode(msg.get("Subject", "")),
        "date": msg.get("Date", ""),
    }


def _find_drafts_folder(conn) -> str:
    """找到草稿箱文件夹名。163 的草稿箱是 UTF-7 编码的。"""
    st, folders = conn.list()
    if st != "OK":
        return "Drafts"
    for f in folders:
        if isinstance(f, bytes):
            decoded = f.decode("utf-8", errors="replace")
            # 163 草稿箱的 UTF-7 编码
            if "&g0l6P3ux-" in decoded:
                return "&g0l6P3ux-"
            lower = decoded.lower()
            if "draft" in lower or "草稿" in lower:
                parts = decoded.split(' "/" ')
                if len(parts) == 2:
                    return parts[1].strip('"')
    return "Drafts"


def _bark_notify(title: str, body: str):
    """发送 Bark 推送通知（best-effort）。"""
    if not BARK_URL:
        return
    try:
        url = BARK_URL.rstrip("/")
        encoded_title = urllib.parse.quote(title, safe="")
        encoded_body = urllib.parse.quote(body, safe="")
        full_url = f"{url}/{encoded_title}/{encoded_body}"
        req = urllib.request.Request(full_url, method="GET")
        urllib.request.urlopen(req, timeout=10)
        log.info(f"Bark notify sent: {title}")
    except Exception as e:
        log.warning(f"Bark notify failed: {e}")


# ── Tools ──────────────────────────────────────────────────────

class InboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, description="获取最近几封邮件", ge=1, le=50)
    folder: str = Field(default="INBOX", description="邮箱文件夹")

@mcp.tool(name="mail_inbox")
async def mail_inbox(params: InboxInput) -> str:
    """查看收件箱最近的邮件列表（标题、发件人、时间）。"""
    try:
        conn = _imap()
        conn.select(params.folder, readonly=True)
        status, data = conn.uid("search", None, "ALL")
        if status != "OK" or not data[0]:
            conn.logout()
            return json.dumps([], ensure_ascii=False)
        uids = data[0].split()[-params.limit:]
        uids.reverse()
        results = []
        for uid in uids:
            uid_str = uid.decode()
            st, md = conn.uid("fetch", uid, "(BODY.PEEK[HEADER])")
            if st == "OK" and md[0]:
                msg = email.message_from_bytes(md[0][1])
                results.append(_summary(msg, uid_str))
        conn.logout()
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {e}"


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: str = Field(..., description="邮件 UID（从 mail_inbox 获取）")
    folder: str = Field(default="INBOX")
    max_chars: int = Field(default=5000, ge=100, le=50000)

@mcp.tool(name="mail_read")
async def mail_read(params: ReadInput) -> str:
    """读取一封邮件的完整内容。"""
    try:
        conn = _imap()
        conn.select(params.folder, readonly=True)
        st, md = conn.uid("fetch", params.uid, "(BODY.PEEK[])")
        conn.logout()
        if st != "OK":
            return "错误: 邮件不存在"
        raw = None
        for item in md:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw = item[1]
                break
        if not raw:
            return "错误: 无法解析"
        msg = email.message_from_bytes(raw)
        result = _summary(msg, params.uid)
        body = _get_body(msg)
        result["body"] = body[:params.max_chars]
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {e}"


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., description='IMAP 搜索条件，如 FROM "xxx" / SUBJECT "hello" / UNSEEN / SINCE 01-Jan-2025')
    folder: str = Field(default="INBOX")
    limit: int = Field(default=10, ge=1, le=50)

@mcp.tool(name="mail_search")
async def mail_search(params: SearchInput) -> str:
    """搜索邮件。支持 IMAP 搜索语法。"""
    try:
        conn = _imap()
        conn.select(params.folder, readonly=True)
        st, data = conn.uid("search", None, params.query)
        if st != "OK" or not data[0]:
            conn.logout()
            return json.dumps([], ensure_ascii=False)
        uids = data[0].split()[-params.limit:]
        uids.reverse()
        results = []
        for uid in uids:
            uid_str = uid.decode()
            st2, md = conn.uid("fetch", uid, "(BODY.PEEK[HEADER])")
            if st2 == "OK" and md[0]:
                msg = email.message_from_bytes(md[0][1])
                results.append(_summary(msg, uid_str))
        conn.logout()
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {e}"


class SendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(..., description="收件人邮箱")
    subject: str = Field(..., description="主题")
    body: str = Field(..., description="正文（纯文本）")
    cc: Optional[str] = Field(default=None, description="抄送，逗号分隔")

@mcp.tool(name="mail_send")
async def mail_send(params: SendInput) -> str:
    """写好邮件并存到草稿箱，等待人工审核后发送。（Railway 等平台封锁了 SMTP 端口，
    所以通过 IMAP 存草稿绕过限制。）"""
    try:
        msg = MIMEMultipart()
        msg["From"] = formataddr((EMAIL.get("display_name", "Claude"), EMAIL["address"]))
        msg["To"] = params.to
        msg["Subject"] = params.subject
        msg["Date"] = formatdate(localtime=True)
        if params.cc:
            msg["Cc"] = params.cc
        msg.attach(MIMEText(params.body, "plain", "utf-8"))

        conn = _imap()
        drafts_folder = _find_drafts_folder(conn)
        log.info(f"Saving draft to folder: {drafts_folder}")

        # 选中草稿箱并写入
        st = conn.select(drafts_folder)
        if st[0] != "OK":
            # 如果选中失败，尝试创建
            conn.create(drafts_folder)
            conn.select(drafts_folder)

        result = conn.append(
            drafts_folder,
            "\\Draft",
            imaplib.Time2Internaldate(datetime.now(timezone.utc)),
            msg.as_bytes()
        )
        conn.logout()

        if result[0] == "OK":
            log.info(f"Draft saved: to={params.to} subject={params.subject}")
            # 发 Bark 通知
            _bark_notify(
                "📬 新草稿待审核",
                f"收件人: {params.to}\n主题: {params.subject}\n请打开163邮箱草稿箱审核并发送"
            )
            return json.dumps({
                "status": "draft_saved",
                "to": params.to,
                "subject": params.subject,
                "message": "邮件已存入草稿箱，请在163邮箱app中打开草稿箱审核并点击发送"
            }, ensure_ascii=False)
        else:
            return f"存草稿失败: {result}"

    except Exception as e:
        log.error(f"Draft save failed: {e}")
        return f"存草稿失败: {e}"


@mcp.tool(name="mail_folders")
async def mail_folders() -> str:
    """列出所有邮箱文件夹。"""
    try:
        conn = _imap()
        st, folders = conn.list()
        conn.logout()
        result = []
        for f in folders:
            if isinstance(f, bytes):
                parts = f.decode("utf-8", errors="replace").split(' "/" ')
                if len(parts) == 2:
                    result.append(parts[1].strip('"'))
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"错误: {e}"


# ── Entry ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    log.info(f"Mail bridge starting: {EMAIL['address']} | IMAP {EMAIL['imap_host']}:{EMAIL.get('imap_port',993)} | draft mode (SMTP bypassed)")

    transport = os.environ.get("MCP_TRANSPORT", "sse")

    if "--streamable-http" in sys.argv:
        transport = "streamable-http"

    mcp.run(transport=transport)
