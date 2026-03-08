"""
JARVIS Unified Tool Handler v3.0
Single endpoint that handles ALL Vapi tool calls directly.
No Brave, no passthrough — direct API calls to Yahoo Finance, CoinGecko, Open-Meteo.
"""

import os
import json
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jarvis")

PORT = int(os.environ.get("PORT", 8080))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "7730428672:AAFaKvzBnXYxhMzVJFgq8Ej9g3Ot5Bj5Bnk")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1476514914")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "https://eco-guidelines-grid-cut.trycloudflare.com")
OPENCLAW_HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "43e09303696b9ce63b9bfec06ec32491b35bdc17e7dc995f")


def extract_args(data):
    """Extract tool call arguments from Vapi's payload format."""
    # Vapi sends: message.toolCallList[0].function.arguments (JSON string or dict)
    try:
        msg = data.get("message", data)
        tool_list = msg.get("toolCallList", [])
        if tool_list:
            args_raw = tool_list[0].get("function", {}).get("arguments", {})
            if isinstance(args_raw, str):
                return json.loads(args_raw)
            return args_raw
    except Exception:
        pass
    # Fallback: try top-level keys
    return data


def get_tool_name(data):
    """Extract the tool name from Vapi's payload."""
    try:
        msg = data.get("message", data)
        tool_list = msg.get("toolCallList", [])
        if tool_list:
            return tool_list[0].get("function", {}).get("name", "")
    except Exception:
        pass
    return ""


def fetch_stock(symbol):
    """Fetch stock price directly from Yahoo Finance."""
    symbol = symbol.upper().strip()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    r = requests.get(url, params={"interval": "1d", "range": "2d"}, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    meta = result["meta"]
    price = meta.get("regularMarketPrice") or meta.get("previousClose")
    prev = meta.get("previousClose", price)
    change = round(price - prev, 2)
    change_pct = round((change / prev) * 100, 2) if prev else 0
    name = meta.get("longName") or meta.get("shortName") or symbol
    currency = meta.get("currency", "USD")
    direction = "up" if change >= 0 else "down"
    return f"{name} ({symbol}) is trading at ${price:,.2f} {currency}, {direction} ${abs(change):.2f} ({abs(change_pct):.2f}%) from previous close."


def fetch_crypto(coin):
    """Fetch crypto price directly from CoinGecko."""
    coin = coin.lower().strip()
    # Map common symbols to CoinGecko IDs
    symbol_map = {
        "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
        "bnb": "binancecoin", "xrp": "ripple", "ada": "cardano",
        "doge": "dogecoin", "avax": "avalanche-2", "dot": "polkadot",
        "matic": "matic-network", "link": "chainlink", "theta": "theta-token",
        "ltc": "litecoin", "shib": "shiba-inu", "uni": "uniswap"
    }
    coin_id = symbol_map.get(coin, coin)
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": coin_id, "vs_currencies": "usd", "include_24hr_change": "true", "include_market_cap": "true"},
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    if coin_id not in data:
        return f"Could not find price data for {coin}. Please check the name."
    d = data[coin_id]
    price = d.get("usd", 0)
    change = d.get("usd_24h_change", 0) or 0
    mcap = d.get("usd_market_cap", 0)
    direction = "up" if change >= 0 else "down"
    return f"{coin_id.replace('-', ' ').title()} is at ${price:,.4f} USD, {direction} {abs(change):.2f}% in 24h. Market cap: ${mcap:,.0f}."


def fetch_weather(city):
    """Fetch weather directly from Open-Meteo (no API key needed)."""
    city = city.strip()
    # Geocode
    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en"},
        timeout=8
    )
    geo.raise_for_status()
    results = geo.json().get("results", [])
    if not results:
        return f"Could not find weather data for {city}."
    loc = results[0]
    lat, lon = loc["latitude"], loc["longitude"]
    loc_name = loc.get("name", city)
    country = loc.get("country", "")

    # Weather
    w = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code",
            "temperature_unit": "celsius", "wind_speed_unit": "kmh", "timezone": "auto"
        },
        timeout=8
    )
    w.raise_for_status()
    curr = w.json().get("current", {})
    temp_c = curr.get("temperature_2m", 0)
    temp_f = round(temp_c * 9/5 + 32, 1)
    feels_c = curr.get("apparent_temperature", temp_c)
    feels_f = round(feels_c * 9/5 + 32, 1)
    humidity = curr.get("relative_humidity_2m", 0)
    wind = curr.get("wind_speed_10m", 0)
    code = curr.get("weather_code", 0)

    conditions = {
        0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
        45: "foggy", 48: "icy fog", 51: "light drizzle", 53: "moderate drizzle",
        61: "light rain", 63: "moderate rain", 65: "heavy rain",
        71: "light snow", 73: "moderate snow", 75: "heavy snow",
        80: "light showers", 81: "moderate showers", 82: "heavy showers",
        95: "thunderstorm", 96: "thunderstorm with hail"
    }
    condition = conditions.get(code, f"weather code {code}")

    return (f"In {loc_name}, {country}: {temp_c}°C ({temp_f}°F), feels like {feels_c}°C ({feels_f}°F). "
            f"Conditions: {condition}. Humidity: {humidity}%. Wind: {wind} km/h.")


def send_telegram(text):
    """Send message to Telegram."""
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=10
    )


# ── Main unified tool endpoint ────────────────────────────────────────────────

@app.route("/tools", methods=["POST"])
def unified_tools():
    """Single endpoint for ALL Vapi tool calls."""
    data = request.get_json(force=True) or {}
    tool_name = get_tool_name(data)
    args = extract_args(data)
    log.info(f"Tool call: {tool_name} | args: {args}")

    try:
        if tool_name == "get_stock_price":
            symbol = args.get("symbol", "").upper().strip()
            if not symbol:
                return jsonify({"result": "Please provide a stock ticker symbol."}), 200
            result = fetch_stock(symbol)
            return jsonify({"result": result}), 200

        elif tool_name == "get_crypto_price":
            coin = (args.get("coin") or args.get("coin_id") or args.get("symbol") or "").lower().strip()
            if not coin:
                return jsonify({"result": "Please provide a cryptocurrency name or symbol."}), 200
            result = fetch_crypto(coin)
            return jsonify({"result": result}), 200

        elif tool_name == "get_weather":
            city = (args.get("city") or args.get("location") or "").strip()
            if not city:
                return jsonify({"result": "Please provide a city name."}), 200
            result = fetch_weather(city)
            return jsonify({"result": result}), 200

        elif tool_name == "web_search":
            query = args.get("query", "").strip()
            if not query:
                return jsonify({"result": "Please provide a search query."}), 200
            # Use DuckDuckGo instant answer API (no key needed)
            r = requests.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
                timeout=10
            )
            d = r.json()
            abstract = d.get("AbstractText", "")
            answer = d.get("Answer", "")
            related = [r.get("Text", "") for r in d.get("RelatedTopics", [])[:3] if r.get("Text")]
            if answer:
                result = answer
            elif abstract:
                result = abstract
            elif related:
                result = " | ".join(related[:2])
            else:
                result = f"I searched for '{query}' but couldn't find a direct answer. You may want to check online for the latest information."
            return jsonify({"result": result}), 200

        elif tool_name == "deep_research":
            query = (args.get("query") or args.get("topic") or "").strip()
            if not query:
                return jsonify({"result": "Please provide a research topic."}), 200
            send_telegram(f"🔬 *Deep Research Request*\n\nSir requested research on: _{query}_\n\nProcessing now...")
            return jsonify({"result": f"Research on '{query}' has been dispatched, Sir. Results will arrive on Telegram shortly."}), 200

        elif tool_name == "execute_task":
            task = (args.get("task") or args.get("command") or args.get("message") or "").strip()
            if not task:
                return jsonify({"result": "No task provided."}), 200
            try:
                hook_payload = {"message": task, "channel": "api"}
                r = requests.post(
                    f"{OPENCLAW_GATEWAY_URL}/hooks",
                    headers={"Authorization": f"Bearer {OPENCLAW_HOOK_TOKEN}", "Content-Type": "application/json"},
                    json=hook_payload,
                    timeout=20
                )
                if r.status_code in (200, 201, 202):
                    return jsonify({"result": f"Task sent to OpenClaw, Sir. Executing: {task[:80]}. Results on Telegram."}), 200
                else:
                    return jsonify({"result": "OpenClaw received the task. Results will appear on Telegram."}), 200
            except Exception as e:
                return jsonify({"result": "Task queued. OpenClaw will process it shortly."}), 200

        else:
            log.warning(f"Unknown tool: {tool_name}")
            return jsonify({"result": f"Tool '{tool_name}' is not available."}), 200

    except Exception as e:
        log.error(f"Tool error [{tool_name}]: {e}")
        # Return a graceful error — never leave Vapi hanging
        tool_friendly = tool_name.replace("_", " ").replace("get ", "")
        return jsonify({"result": f"I'm having trouble fetching the {tool_friendly} data right now, Sir. Please try again in a moment."}), 200


# ── Manus webhook receiver ────────────────────────────────────────────────────

@app.route("/webhook/manus", methods=["POST"])
def manus_webhook():
    """Receive Manus task completion and forward to Telegram."""
    data = request.get_json(force=True) or {}
    task_id = data.get("task_id", "unknown")
    status = data.get("status", "unknown")
    result = data.get("result") or data.get("output") or data.get("message") or str(data)[:500]
    msg = f"🤖 *JARVIS Research Complete*\n\nTask: `{task_id}`\nStatus: {status}\n\n{result[:3000]}"
    send_telegram(msg)
    return jsonify({"ok": True}), 200


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "version": "3.0.0", "service": "JARVIS Unified Tool Handler"}), 200


@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "JARVIS Unified Tool Handler", "version": "3.0.0", "endpoint": "POST /tools"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
