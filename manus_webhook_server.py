"""
JARVIS Unified Tool Handler v5.1.0
Correct Vapi response format: {"results": [{"toolCallId": "...", "result": "..."}]}
Direct API calls - no passthrough, no Brave.
Fix: Added /vapi-events endpoint for serverUrl. All 6 tools point to /tools.
"""

import os
import json
import logging
import requests
import threading
from openai import OpenAI
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jarvis")

PORT = int(os.environ.get("PORT", 8080))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8430951431:AAE3Jl_WI9tvbbe-Y2PjoYgDCnSDhCx1ZTA")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "623939621")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "https://eco-guidelines-grid-cut.trycloudflare.com")
OPENCLAW_HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "43e09303696b9ce63b9bfec06ec32491b35bdc17e7dc995f")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "BSAjBNPwOGXAxrOvBeujPlitG43sgEv")


def vapi_response(tool_call_id, result_text):
    """Return the exact format Vapi requires for tool results."""
    return jsonify({
        "results": [
            {
                "toolCallId": tool_call_id,
                "result": result_text
            }
        ]
    }), 200


def extract_tool_info(data):
    """Extract tool name, arguments, and call ID from Vapi's payload."""
    try:
        msg = data.get("message", data)
        tool_list = msg.get("toolCallList", [])
        if tool_list:
            call = tool_list[0]
            call_id = call.get("id", "unknown")
            fn = call.get("function", {})
            name = fn.get("name", "")
            args_raw = fn.get("arguments", {})
            if isinstance(args_raw, str):
                args = json.loads(args_raw)
            else:
                args = args_raw
            return name, args, call_id
    except Exception as e:
        log.error(f"extract_tool_info error: {e}")
    return "", {}, "unknown"


def fetch_stock(symbol):
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
    coin = coin.lower().strip()
    symbol_map = {
        "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
        "bnb": "binancecoin", "xrp": "ripple", "ada": "cardano",
        "doge": "dogecoin", "avax": "avalanche-2", "dot": "polkadot",
        "matic": "matic-network", "link": "chainlink", "theta": "theta-token",
        "ltc": "litecoin", "shib": "shiba-inu", "uni": "uniswap",
        "stx": "blockstack", "stacks": "blockstack",
        "atom": "cosmos", "near": "near", "icp": "internet-computer",
        "ftm": "fantom", "algo": "algorand", "xlm": "stellar",
        "hbar": "hedera-hashgraph", "egld": "elrond-erd-2",
        "sand": "the-sandbox", "mana": "decentraland",
        "grt": "the-graph", "axs": "axie-infinity"
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
        return f"Could not find price data for {coin}."
    d = data[coin_id]
    price = d.get("usd", 0)
    change = d.get("usd_24h_change", 0) or 0
    mcap = d.get("usd_market_cap", 0)
    direction = "up" if change >= 0 else "down"
    return f"{coin_id.replace('-', ' ').title()} is at ${price:,.4f} USD, {direction} {abs(change):.2f}% in 24h. Market cap: ${mcap:,.0f}."


def fetch_weather(city):
    city = city.strip()
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
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")


@app.route("/tools", methods=["POST"])
def unified_tools():
    """Single endpoint for ALL Vapi tool calls. Returns Vapi-required results array format."""
    data = request.get_json(force=True) or {}
    tool_name, args, call_id = extract_tool_info(data)
    log.info(f"Tool: {tool_name} | ID: {call_id} | Args: {args}")

    try:
        if tool_name == "get_stock_price":
            symbol = args.get("symbol", "").upper().strip()
            if not symbol:
                return vapi_response(call_id, "Please provide a stock ticker symbol.")
            result = fetch_stock(symbol)
            return vapi_response(call_id, result)

        elif tool_name == "get_crypto_price":
            coin = (args.get("coin") or args.get("coin_id") or args.get("symbol") or "").lower().strip()
            if not coin:
                return vapi_response(call_id, "Please provide a cryptocurrency name or symbol.")
            result = fetch_crypto(coin)
            return vapi_response(call_id, result)

        elif tool_name == "get_weather":
            city = (args.get("city") or args.get("location") or "").strip()
            if not city:
                return vapi_response(call_id, "Please provide a city name.")
            result = fetch_weather(city)
            return vapi_response(call_id, result)

        elif tool_name == "web_search":
            query = args.get("query", "").strip()
            if not query:
                return vapi_response(call_id, "Please provide a search query.")
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
                params={"q": query, "count": 5, "text_decorations": False},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            web_results = data.get("web", {}).get("results", [])
            if web_results:
                snippets = []
                for wr in web_results[:3]:
                    title = wr.get("title", "")
                    desc = wr.get("description", "")
                    if desc:
                        snippets.append(f"{title}: {desc}")
                result = " | ".join(snippets) if snippets else "No results found."
            else:
                result = f"No web results found for '{query}'."
            return vapi_response(call_id, result)

        elif tool_name == "get_crypto_rank":
            rank = args.get("rank", 0)
            try:
                rank = int(rank)
            except Exception:
                return vapi_response(call_id, "Please provide a valid rank number.")
            per_page = min(250, max(rank + 5, 50))
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": per_page, "page": 1, "sparkline": False},
                timeout=15
            )
            r.raise_for_status()
            coins = r.json()
            if rank <= len(coins):
                coin = coins[rank - 1]
                name = coin.get("name", "Unknown")
                symbol = coin.get("symbol", "").upper()
                price = coin.get("current_price", 0)
                mcap = coin.get("market_cap", 0)
                change = coin.get("price_change_percentage_24h", 0) or 0
                direction = "up" if change >= 0 else "down"
                result = (f"The #{rank} cryptocurrency by market cap is {name} ({symbol}), "
                         f"trading at ${price:,.4f} USD, {direction} {abs(change):.2f}% in 24h. "
                         f"Market cap: ${mcap:,.0f}.")
            else:
                result = f"Could not retrieve the #{rank} cryptocurrency ranking."
            return vapi_response(call_id, result)

        elif tool_name == "deep_research":
            query = (args.get("query") or args.get("topic") or "").strip()
            if not query:
                return vapi_response(call_id, "Please provide a research topic.")
            send_telegram(f"🔬 *Deep Research Request*\n\nSir requested research on: _{query}_\n\nProcessing now...")
            return vapi_response(call_id, f"Research on '{query}' has been dispatched, Sir. Results will arrive on Telegram shortly.")

        elif tool_name == "execute_task":
            task = (args.get("task") or args.get("command") or args.get("message") or "").strip()
            if not task:
                return vapi_response(call_id, "No task provided.")
            try:
                hook_payload = {"message": task, "channel": "api"}
                r = requests.post(
                    f"{OPENCLAW_GATEWAY_URL}/hooks",
                    headers={"Authorization": f"Bearer {OPENCLAW_HOOK_TOKEN}", "Content-Type": "application/json"},
                    json=hook_payload,
                    timeout=20
                )
                return vapi_response(call_id, f"Task sent to OpenClaw, Sir. Executing: {task[:80]}. Results on Telegram.")
            except Exception:
                return vapi_response(call_id, "Task queued. OpenClaw will process it shortly.")

        else:
            log.warning(f"Unknown tool: {tool_name}")
            return vapi_response(call_id, f"Tool '{tool_name}' is not available.")

    except Exception as e:
        log.error(f"Tool error [{tool_name}]: {e}")
        tool_friendly = tool_name.replace("_", " ").replace("get ", "") if tool_name else "requested"
        return vapi_response(call_id, f"I'm having trouble fetching the {tool_friendly} data right now, Sir. Please try again in a moment.")


@app.route("/vapi-events", methods=["POST"])
def vapi_events():
    """Handle Vapi server-url events (call status, transcripts, end-of-call, etc.)
    This endpoint is set as the assistant's serverUrl in Vapi.
    It must return 200 quickly to avoid call drops."""
    data = request.get_json(force=True) or {}
    msg = data.get("message", {})
    event_type = msg.get("type", "unknown")
    call_info = msg.get("call", {})
    call_id = call_info.get("id", "unknown") if isinstance(call_info, dict) else "unknown"
    log.info(f"Vapi server event: {event_type} | call: {call_id}")
    # Always return 200 OK immediately for all server events
    return jsonify({"received": True}), 200


@app.route("/webhook/manus", methods=["POST"])
def manus_webhook():
    data = request.get_json(force=True) or {}
    log.info(f"Received Manus webhook: {json.dumps(data)}")
    
    event_type = data.get("event_type", "unknown")
    
    # We only care about task_stopped for final results
    if event_type == "task_stopped":
        task_detail = data.get("task_detail", {})
        task_id = task_detail.get("task_id", "unknown")
        stop_reason = task_detail.get("stop_reason", "unknown")
        message = task_detail.get("message", "No message provided")
        
        # Format the final message for Telegram
        msg = f"🤖 *JARVIS Deep Research Complete*\n\n"
        msg += f"Task: `{task_id}`\n"
        msg += f"Status: {stop_reason}\n\n"
        msg += f"{message[:3500]}" # Telegram has a 4096 char limit
        
        # Check for attachments
        attachments = task_detail.get("attachments", [])
        if attachments:
            msg += "\n\n*Attachments:*\n"
            for att in attachments:
                file_name = att.get("file_name", "file")
                url = att.get("url", "")
                if url:
                    msg += f"- [{file_name}]({url})\n"
                    
        send_telegram(msg)
        
    elif event_type == "task_progress":
        # Optional: could send progress updates to Telegram, but might be too spammy
        # Just log it for now
        progress = data.get("progress_detail", {})
        log.info(f"Task progress: {progress.get('message', '')}")
        
    return jsonify({"ok": True}), 200


def process_research_async(query, chat_id, bot_token):
    try:
        manus_api_key = os.environ.get("MANUS_API_KEY", "")
        if not manus_api_key:
            raise ValueError("MANUS_API_KEY environment variable is not set")
            
        # Send initial acknowledgment to Telegram
        header = f"🔬 *JARVIS Deep Research Initiated*\n\nTopic: _{query}_\n\nDispatching to Manus Agent..."
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": header, "parse_mode": "Markdown"},
            timeout=10
        )
        
        prompt = (
            f"Perform comprehensive web research on the following topic and generate a detailed, "
            f"well-structured report. Cover: background/overview, key facts, current status, "
            f"analysis, pros/cons or risks/opportunities, and a conclusion/outlook.\n\n"
            f"Topic: {query}\n\n"
            f"Format the report with clear section headers using ALL CAPS followed by a colon "
            f"(e.g. OVERVIEW:, KEY FACTS:, ANALYSIS:). Use plain text only - no asterisks, "
            f"no markdown, no hashtags. Write in full paragraphs."
        )
        
        # Create a task using Manus API
        manus_url = "https://api.manus.ai/v1/tasks"
        headers = {
            "API_KEY": manus_api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "prompt": prompt,
            "agentProfile": "manus-1.6-max"
        }
        
        log.info(f"Sending request to Manus API for query: {query}")
        response = requests.post(manus_url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        task_id = data.get("task_id", "unknown")
        task_url = data.get("task_url", "")
        
        # Register webhook for this task completion
        webhook_url = "https://api.manus.ai/v1/webhooks"
        webhook_payload = {
            "webhook": {
                "url": "https://jarvis-webhook-production.up.railway.app/webhook/manus"
            }
        }
        
        try:
            wh_response = requests.post(webhook_url, headers=headers, json=webhook_payload, timeout=10)
            wh_response.raise_for_status()
            log.info("Webhook registered successfully")
        except Exception as wh_e:
            log.error(f"Failed to register webhook (might already exist): {wh_e}")
        
        # Notify user that task is created
        msg = f"✅ *Task Created Successfully*\n\nTask ID: `{task_id}`\n\nI will notify you here once the comprehensive research is complete."
        if task_url:
            msg += f"\n\nTrack progress: [View Task]({task_url})"
            
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10
        )
            
    except Exception as e:
        log.error(f"Async research error: {e}")
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"Research error for '{query}': {str(e)}"},
                timeout=10
            )
        except Exception:
            pass

@app.route("/research", methods=["POST"])
def research_endpoint():
    data = request.get_json(force=True) or {}
    query = data.get("query")
    chat_id = data.get("chat_id")
    bot_token = data.get("bot_token")
    
    if not query or not chat_id or not bot_token:
        return jsonify({"error": "Missing required fields: query, chat_id, bot_token"}), 400
    
    log.info(f"Research dispatched: {query[:80]}")
    
    # Start async processing in background thread
    thread = threading.Thread(
        target=process_research_async,
        args=(query, chat_id, bot_token),
        daemon=True
    )
    thread.start()
    
    # Return immediately to avoid Vapi timeout
    return jsonify({"status": "dispatched"}), 200

@app.route("/health", methods=["GET"])
def health():
    manus_key_set = bool(os.environ.get("MANUS_API_KEY", ""))
    return jsonify({"status": "healthy", "version": "5.1.0", "service": "JARVIS Unified Tool Handler", "features": ["manus-deep-research", "telegram-delivery"], "manus_api_key_configured": manus_key_set}), 200


@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "JARVIS Unified Tool Handler", "version": "5.1.0", "endpoints": {"tools": "POST /tools", "events": "POST /vapi-events"}, "features": ["manus-deep-research", "telegram-delivery"]}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
