#!/usr/bin/env python3
"""
JARVIS — Manus Webhook → Telegram Delivery Server
===================================================
Receives Manus task lifecycle events and forwards completed research
results to a Telegram chat.

Environment Variables (set in Railway dashboard):
  MANUS_API_KEY         - Your Manus API key
  BOT_TOKEN             - Telegram bot token
  TELEGRAM_CHAT         - Telegram chat ID
  REGISTERED_WEBHOOK_URL - The full public URL of this server + /webhook/manus
  PORT                  - Port to listen on (Railway sets this automatically)
"""

import base64
import hashlib
import json
import logging
import os
import time
import requests

from flask import Flask, request, jsonify
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature

# ─── Configuration (from environment variables) ───────────────────────────────
MANUS_API_KEY           = os.environ.get("MANUS_API_KEY", "")
MANUS_BASE              = "https://api.manus.ai"
BOT_TOKEN               = os.environ.get("BOT_TOKEN", "")
TELEGRAM_CHAT           = os.environ.get("TELEGRAM_CHAT", "")
PORT                    = int(os.environ.get("PORT", 8765))
REGISTERED_WEBHOOK_URL  = os.environ.get("REGISTERED_WEBHOOK_URL", "")
SKIP_SIG_VERIFY         = os.environ.get("SKIP_SIG_VERIFY", "false").lower() == "true"

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("jarvis-webhook")

# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─── Public Key Cache ─────────────────────────────────────────────────────────
_public_key_cache = {"key": None, "fetched_at": 0, "ttl": 3600}

def get_manus_public_key():
    now = time.time()
    if _public_key_cache["key"] and (now - _public_key_cache["fetched_at"]) < _public_key_cache["ttl"]:
        return _public_key_cache["key"]
    try:
        r = requests.get(
            f"{MANUS_BASE}/v1/webhook/public_key",
            headers={"API_KEY": MANUS_API_KEY, "accept": "application/json"},
            timeout=10
        )
        if r.status_code == 200:
            key_pem = r.json().get("public_key")
            _public_key_cache["key"] = key_pem
            _public_key_cache["fetched_at"] = now
            log.info("Manus public key fetched and cached")
            return key_pem
        else:
            log.warning(f"Failed to fetch public key: {r.status_code}")
            return None
    except Exception as e:
        log.warning(f"Error fetching public key: {e}")
        return None


def verify_signature(req):
    if SKIP_SIG_VERIFY:
        return True

    sig_b64   = req.headers.get("X-Webhook-Signature")
    timestamp = req.headers.get("X-Webhook-Timestamp")

    if not sig_b64 or not timestamp:
        log.warning("Missing signature headers")
        return False

    try:
        age = abs(int(time.time()) - int(timestamp))
        if age > 300:
            log.warning(f"Stale timestamp ({age}s old)")
            return False
    except ValueError:
        return False

    public_key_pem = get_manus_public_key()
    if not public_key_pem:
        return True  # Fail open if key unavailable

    body_bytes   = req.get_data()
    body_hash    = hashlib.sha256(body_bytes).hexdigest()
    full_url     = REGISTERED_WEBHOOK_URL
    content_str  = f"{timestamp}.{full_url}.{body_hash}"
    content_bytes = content_str.encode("utf-8")

    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        signature  = base64.b64decode(sig_b64)
        public_key.verify(signature, content_bytes, padding.PKCS1v15(), hashes.SHA256())
        log.info("Signature verified ✅")
        return True
    except InvalidSignature:
        log.warning("Invalid signature")
        return False
    except Exception as e:
        log.warning(f"Signature error: {e}")
        return True


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info(f"Telegram message sent (id={r.json()['result']['message_id']})")
            return True
        else:
            log.error(f"Telegram error: {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def format_task_stopped(payload):
    detail     = payload.get("task_detail", {})
    task_title = detail.get("task_title", "Research Task")
    task_url   = detail.get("task_url", "")
    message    = detail.get("message", "No summary available.")
    stop_reason = detail.get("stop_reason", "finish")
    attachments = detail.get("attachments", [])
    task_id    = detail.get("task_id", "unknown")

    if len(message) > 800:
        message = message[:800] + "…"

    status_label = "Complete" if stop_reason == "finish" else "Awaiting Input"

    lines = [
        f"🤖 <b>JARVIS Research {status_label}:</b>",
        "",
        f"📋 <b>{task_title}</b>",
        "",
        message,
    ]

    if task_url:
        lines += ["", f"🔗 <a href=\"{task_url}\">View Full Report</a>"]

    if attachments:
        lines += ["", f"📎 <b>Attachments ({len(attachments)}):</b>"]
        for att in attachments[:5]:
            name = att.get("file_name", "file")
            url  = att.get("url", "")
            size = att.get("size_bytes", 0)
            size_str = f"{size // 1024}KB" if size > 0 else ""
            if url:
                lines.append(f"  • <a href=\"{url}\">{name}</a> {size_str}".strip())

    lines += ["", f"<i>Task ID: {task_id}</i>"]
    return "\n".join(lines)


@app.route("/webhook/manus", methods=["POST"])
def manus_webhook():
    log.info(f"Incoming webhook — {request.method} {request.path}")

    if not verify_signature(request):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        payload = request.get_json(force=True)
    except Exception as e:
        return jsonify({"error": "Bad request"}), 400

    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    event_type = payload.get("event_type", "unknown")
    log.info(f"Event: {event_type}")

    if event_type == "task_stopped":
        tg_message = format_task_stopped(payload)
        send_telegram(tg_message)

    return jsonify({"status": "ok", "event_type": event_type}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "service": "JARVIS Manus Webhook Server",
        "version": "1.0.0",
        "port": PORT
    }), 200


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "JARVIS Manus Webhook Server",
        "endpoints": {"/webhook/manus": "POST", "/health": "GET"}
    }), 200


if __name__ == "__main__":
    log.info("JARVIS Manus Webhook Server starting...")
    key = get_manus_public_key()
    if key:
        log.info("Manus RSA public key pre-loaded ✅")
    app.run(host="0.0.0.0", port=PORT, debug=False)
