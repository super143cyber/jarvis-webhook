#!/usr/bin/env python3
"""
JARVIS — Manus Webhook + Tool Proxy Server
============================================
1. Receives Manus task lifecycle events → forwards to Telegram
2. Proxies Vapi tool calls to external APIs (Brave, CoinGecko, Yahoo, Open-Meteo)

Vapi sends tool calls as POST with JSON body, but many APIs expect GET with
query params. This server bridges that gap.

Environment Variables (set in Railway dashboard):
  MANUS_API_KEY          - Your Manus API key
  BOT_TOKEN              - Telegram bot token
  TELEGRAM_CHAT          - Telegram chat ID
  REGISTERED_WEBHOOK_URL - Full public URL of /webhook/manus endpoint
  BRAVE_API_KEY          - Brave Search API key
  PORT                   - Port to listen on (Railway sets this automatically)
"""

import base64
import hashlib
import json
import logging
import os
import time
import urllib.parse
import requests

from flask import Flask, request, jsonify
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature

# ─── Configuration ────────────────────────────────────────────────────────────
MANUS_API_KEY           = os.environ.get("MANUS_API_KEY", "")
MANUS_BASE              = "https://api.manus.ai"
BOT_TOKEN               = os.environ.get("BOT_TOKEN", "")
TELEGRAM_CHAT           = os.environ.get("TELEGRAM_CHAT", "")
PORT                    = int(os.environ.get("PORT", 8765))
REGISTERED_WEBHOOK_URL  = os.environ.get("REGISTERED_WEBHOOK_URL",
    "https://jarvis-webhook-production.up.railway.app/webhook/manus")
SKIP_SIG_VERIFY         = os.environ.get("SKIP_SIG_VERIFY", "false").lower() == "true"
BRAVE_API_KEY           = os.environ.get("BRAVE_API_KEY", "BSAjBNPwOGXAxrOvBeujPlitG43sgEv")

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("jarvis-webhook")

# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  MANUS WEBHOOK (existing functionality)
# ═══════════════════════════════════════════════════════════════════════════════

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
    except Exception as e:
        log.warning(f"Error fetching public key: {e}")
    return _public_key_cache.get("key")


def verify_signature(req):
    if SKIP_SIG_VERIFY:
        return True
    sig_b64   = req.headers.get("X-Webhook-Signature")
    timestamp = req.headers.get("X-Webhook-Timestamp")
    if not sig_b64 or not timestamp:
        return False
    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    public_key_pem = get_manus_public_key()
    if not public_key_pem:
        return True
    body_bytes   = req.get_data()
    body_hash    = hashlib.sha256(body_bytes).hexdigest()
    content_str  = f"{timestamp}.{REGISTERED_WEBHOOK_URL}.{body_hash}"
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        signature  = base64.b64decode(sig_b64)
        public_key.verify(signature, content_str.encode("utf-8"),
                          padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False
    except Exception:
        return True


def send_telegram(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=10
        )
        if r.status_code == 200:
            log.info(f"Telegram sent (id={r.json()['result']['message_id']})")
            return True
        log.error(f"Telegram error: {r.status_code}")
        return False
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False


def format_task_stopped(payload):
    detail      = payload.get("task_detail", {})
    task_title  = detail.get("task_title", "Research Task")
    task_url    = detail.get("task_url", "")
    message     = detail.get("message", "No summary available.")
    stop_reason = detail.get("stop_reason", "finish")
    attachments = detail.get("attachments", [])
    task_id     = detail.get("task_id", "unknown")
    if len(message) > 800:
        message = message[:800] + "..."
    status = "Complete" if stop_reason == "finish" else "Awaiting Input"
    lines = [f"🤖 <b>JARVIS Research {status}:</b>", "",
             f"📋 <b>{task_title}</b>", "", message]
    if task_url:
        lines += ["", f'🔗 <a href="{task_url}">View Full Report</a>']
    if attachments:
        lines += ["", f"📎 <b>Attachments ({len(attachments)}):</b>"]
        for att in attachments[:5]:
            name = att.get("file_name", "file")
            url  = att.get("url", "")
            if url:
                lines.append(f'  - <a href="{url}">{name}</a>')
    lines += ["", f"<i>Task ID: {task_id}</i>"]
    return "\n".join(lines)


@app.route("/webhook/manus", methods=["POST"])
def manus_webhook():
    log.info(f"Webhook: {request.method} {request.path}")
    if not verify_signature(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Bad request"}), 400
    if not payload:
        return jsonify({"error": "Empty payload"}), 400
    event_type = payload.get("event_type", "unknown")
    log.info(f"Event: {event_type}")
    if event_type == "task_stopped":
        send_telegram(format_task_stopped(payload))
    return jsonify({"status": "ok", "event_type": event_type}), 200


# ═══════════════════════════════════════════════════════════════════════════════
#  VAPI TOOL PROXY ENDPOINTS
#  Vapi sends POST with JSON body → these endpoints call external APIs
#  and return results as JSON that Vapi reads back to the LLM.
# ═══════════════════════════════════════════════════════════════════════════════

# ─── 1. Web Search (Brave Search API) ────────────────────────────────────────
@app.route("/search", methods=["POST"])
def search_proxy():
    """
    Accepts: POST {"message": {"query": "..."}}
    Calls: Brave Search API (GET with query params)
    Returns: Top 5 results as clean text for voice
    """
    log.info("Tool call: /search")
    try:
        data = request.get_json(force=True)
        # Vapi sends tool args inside message.query or at top level
        msg = data.get("message", data)
        query = msg.get("query", "")
        if not query:
            return jsonify({"results": [{"error": "No query provided"}]}), 200

        log.info(f"Brave search: {query}")
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 5},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": BRAVE_API_KEY
            },
            timeout=10
        )

        if r.status_code != 200:
            log.error(f"Brave API error: {r.status_code} {r.text[:200]}")
            return jsonify({"results": [{"error": f"Search API returned {r.status_code}"}]}), 200

        brave_data = r.json()
        web_results = brave_data.get("web", {}).get("results", [])

        results = []
        for item in web_results[:5]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", "")
            })

        # Also build a clean text summary for voice
        summary_parts = []
        for i, item in enumerate(results, 1):
            summary_parts.append(
                f"{i}. {item['title']}: {item['description']}"
            )
        summary = "\n".join(summary_parts) if summary_parts else "No results found."

        return jsonify({
            "results": results,
            "summary": summary,
            "query": query,
            "count": len(results)
        }), 200

    except Exception as e:
        log.error(f"Search error: {e}")
        return jsonify({"results": [{"error": str(e)}]}), 200


# ─── 2. Crypto Prices (CoinGecko API) ────────────────────────────────────────
@app.route("/crypto", methods=["POST"])
def crypto_proxy():
    """
    Accepts: POST {"message": {"coin_id": "bitcoin", "currency": "usd"}}
    Calls: CoinGecko simple/price API (GET)
    Returns: Price data as JSON
    """
    log.info("Tool call: /crypto")
    try:
        data = request.get_json(force=True)
        msg = data.get("message", data)
        coin_id  = msg.get("coin_id", "bitcoin").lower().strip()
        currency = msg.get("currency", "usd").lower().strip()

        log.info(f"CoinGecko: {coin_id} in {currency}")
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": currency,
                "include_24hr_change": "true",
                "include_market_cap": "true"
            },
            headers={"Accept": "application/json"},
            timeout=10
        )

        if r.status_code != 200:
            return jsonify({"error": f"CoinGecko API returned {r.status_code}"}), 200

        cg_data = r.json()
        coin_data = cg_data.get(coin_id, {})

        if not coin_data:
            return jsonify({
                "error": f"Coin '{coin_id}' not found. Try common IDs like: bitcoin, ethereum, solana, dogecoin"
            }), 200

        price      = coin_data.get(currency, 0)
        change_24h = coin_data.get(f"{currency}_24h_change", 0)
        market_cap = coin_data.get(f"{currency}_market_cap", 0)

        direction = "up" if change_24h and change_24h > 0 else "down"
        summary = (
            f"{coin_id.title()} is currently ${price:,.2f} {currency.upper()}, "
            f"{direction} {abs(change_24h or 0):.2f}% in the last 24 hours. "
            f"Market cap: ${market_cap:,.0f} {currency.upper()}."
        )

        return jsonify({
            "coin": coin_id,
            "currency": currency,
            "price": price,
            "change_24h_percent": round(change_24h or 0, 2),
            "market_cap": market_cap,
            "summary": summary
        }), 200

    except Exception as e:
        log.error(f"Crypto error: {e}")
        return jsonify({"error": str(e)}), 200


# ─── 3. Stock Prices (Yahoo Finance API) ─────────────────────────────────────
@app.route("/stock", methods=["POST"])
def stock_proxy():
    """
    Accepts: POST {"message": {"symbol": "AAPL"}}
    Calls: Yahoo Finance v8 quote API (GET)
    Returns: Stock price and key metrics
    """
    log.info("Tool call: /stock")
    try:
        data = request.get_json(force=True)
        msg = data.get("message", data)
        symbol = msg.get("symbol", "AAPL").upper().strip()

        log.info(f"Yahoo Finance: {symbol}")
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1d", "range": "2d"},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; JARVIS/1.0)",
                "Accept": "application/json"
            },
            timeout=10
        )

        if r.status_code != 200:
            return jsonify({"error": f"Yahoo Finance returned {r.status_code} for {symbol}"}), 200

        yf_data = r.json()
        result = yf_data.get("chart", {}).get("result", [])
        if not result:
            return jsonify({"error": f"No data found for symbol '{symbol}'"}), 200

        meta = result[0].get("meta", {})
        price          = meta.get("regularMarketPrice", 0)
        prev_close     = meta.get("previousClose", meta.get("chartPreviousClose", 0))
        currency       = meta.get("currency", "USD")
        exchange       = meta.get("exchangeName", "")
        name           = meta.get("shortName", meta.get("longName", symbol))

        change = price - prev_close if prev_close else 0
        change_pct = (change / prev_close * 100) if prev_close else 0
        direction = "up" if change >= 0 else "down"

        summary = (
            f"{name} ({symbol}) is trading at ${price:,.2f} {currency}, "
            f"{direction} ${abs(change):,.2f} ({abs(change_pct):.2f}%) "
            f"from previous close on {exchange}."
        )

        return jsonify({
            "symbol": symbol,
            "name": name,
            "price": round(price, 2),
            "previous_close": round(prev_close, 2),
            "change": round(change, 2),
            "change_percent": round(change_pct, 2),
            "currency": currency,
            "exchange": exchange,
            "summary": summary
        }), 200

    except Exception as e:
        log.error(f"Stock error: {e}")
        return jsonify({"error": str(e)}), 200


# ─── 4. Weather (Open-Meteo API) ─────────────────────────────────────────────
@app.route("/weather", methods=["POST"])
def weather_proxy():
    """
    Accepts: POST {"message": {"location": "Toronto"}}
    Calls: Open-Meteo geocoding + weather API (GET)
    Returns: Current weather conditions
    """
    log.info("Tool call: /weather")
    try:
        data = request.get_json(force=True)
        msg = data.get("message", data)
        location = msg.get("location", "New York").strip()

        log.info(f"Weather for: {location}")

        # Step 1: Geocode the location
        geo_r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en"},
            timeout=10
        )
        if geo_r.status_code != 200:
            return jsonify({"error": f"Geocoding failed for '{location}'"}), 200

        geo_data = geo_r.json()
        results = geo_data.get("results", [])
        if not results:
            return jsonify({"error": f"Location '{location}' not found"}), 200

        place = results[0]
        lat   = place["latitude"]
        lon   = place["longitude"]
        city  = place.get("name", location)
        country = place.get("country", "")

        # Step 2: Get current weather
        wx_r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "wind_speed_10m,weather_code",
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh"
            },
            timeout=10
        )
        if wx_r.status_code != 200:
            return jsonify({"error": "Weather API failed"}), 200

        wx_data = wx_r.json()
        current = wx_data.get("current", {})

        temp       = current.get("temperature_2m", 0)
        feels_like = current.get("apparent_temperature", 0)
        humidity   = current.get("relative_humidity_2m", 0)
        wind       = current.get("wind_speed_10m", 0)
        code       = current.get("weather_code", 0)

        # Weather code to description
        wx_codes = {
            0: "clear sky", 1: "mainly clear", 2: "partly cloudy",
            3: "overcast", 45: "foggy", 48: "depositing rime fog",
            51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
            61: "slight rain", 63: "moderate rain", 65: "heavy rain",
            71: "slight snow", 73: "moderate snow", 75: "heavy snow",
            77: "snow grains", 80: "slight rain showers", 81: "moderate rain showers",
            82: "violent rain showers", 85: "slight snow showers",
            86: "heavy snow showers", 95: "thunderstorm",
            96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail"
        }
        condition = wx_codes.get(code, f"weather code {code}")

        temp_f = temp * 9 / 5 + 32
        feels_f = feels_like * 9 / 5 + 32

        summary = (
            f"Current weather in {city}, {country}: {condition}. "
            f"Temperature is {temp:.1f}°C ({temp_f:.0f}°F), "
            f"feels like {feels_like:.1f}°C ({feels_f:.0f}°F). "
            f"Humidity {humidity}%, wind {wind:.0f} km/h."
        )

        return jsonify({
            "location": f"{city}, {country}",
            "temperature_c": round(temp, 1),
            "temperature_f": round(temp_f, 1),
            "feels_like_c": round(feels_like, 1),
            "feels_like_f": round(feels_f, 1),
            "humidity_percent": humidity,
            "wind_speed_kmh": round(wind, 1),
            "condition": condition,
            "summary": summary
        }), 200

    except Exception as e:
        log.error(f"Weather error: {e}")
        return jsonify({"error": str(e)}), 200


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH & ROOT
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "service": "JARVIS Webhook + Tool Proxy Server",
        "version": "2.0.0",
        "port": PORT,
        "endpoints": {
            "/webhook/manus": "POST — Manus task events → Telegram",
            "/search":  "POST — Brave web search proxy",
            "/crypto":  "POST — CoinGecko crypto price proxy",
            "/stock":   "POST — Yahoo Finance stock price proxy",
            "/weather": "POST — Open-Meteo weather proxy"
        }
    }), 200


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "JARVIS Webhook + Tool Proxy Server",
        "version": "2.0.0",
        "endpoints": {
            "GET  /":               "This page",
            "GET  /health":         "Health check",
            "POST /webhook/manus":  "Manus webhook receiver",
            "POST /search":         "Web search (Brave)",
            "POST /crypto":         "Crypto prices (CoinGecko)",
            "POST /stock":          "Stock prices (Yahoo Finance)",
            "POST /weather":        "Weather (Open-Meteo)"
        }
    }), 200



# ── OpenClaw proxy ──────────────────────────────────────────────────────────
OPENCLAW_GATEWAY_URL = "https://eco-guidelines-grid-cut.trycloudflare.com"
OPENCLAW_HOOK_TOKEN  = "43e09303696b9ce63b9bfec06ec32491b35bdc17e7dc995f"

@app.route("/openclaw", methods=["POST"])
def openclaw_proxy():
    """Proxy Vapi tool calls to OpenClaw gateway /hooks endpoint."""
    data = request.get_json(force=True) or {}
    task = data.get("task", "")
    if not task:
        return jsonify({"error": "No task provided", "summary": "No task was specified."}), 400

    log.info(f"OpenClaw task: {task[:100]}")
    try:
        hook_payload = {"message": task, "channel": "api"}
        r = requests.post(
            f"{OPENCLAW_GATEWAY_URL}/hooks",
            headers={
                "Authorization": f"Bearer {OPENCLAW_HOOK_TOKEN}",
                "Content-Type": "application/json"
            },
            json=hook_payload,
            timeout=25
        )
        if r.status_code in (200, 201, 202):
            return jsonify({
                "success": True,
                "summary": f"Task sent to OpenClaw successfully. OpenClaw is now executing: {task[:100]}. Results will appear in Telegram shortly."
            }), 200
        else:
            log.error(f"OpenClaw error {r.status_code}: {r.text[:200]}")
            return jsonify({
                "success": False,
                "summary": f"OpenClaw returned an error ({r.status_code}). The task may not have been executed."
            }), 200
    except Exception as e:
        log.error(f"OpenClaw proxy exception: {e}")
        return jsonify({
            "success": False,
            "summary": "Could not reach OpenClaw. The gateway may be offline."
        }), 200

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  JARVIS Webhook + Tool Proxy Server v2.0.0")
    log.info(f"  Port: {PORT}")
    log.info(f"  Telegram: {TELEGRAM_CHAT}")
    log.info(f"  Brave key: {'set' if BRAVE_API_KEY else 'NOT SET'}")
    log.info("=" * 60)
    get_manus_public_key()
    app.run(host="0.0.0.0", port=PORT, debug=False)
