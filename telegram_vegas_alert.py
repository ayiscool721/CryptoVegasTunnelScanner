#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_vegas_alert.py
===================================================================
維加斯通道 - 多週期完全一致 Telegram 通知腳本

這是一支「獨立於 Streamlit App」運作的腳本，設計給 GitHub Actions 排程呼叫
（建議每15分鐘一次，對齊每個整點的0/15/30/45分），跟你平常用瀏覽器打開的
Streamlit互動掃描App是兩個完全分開的東西：
- Streamlit App：你手動點「開始掃描」時才運作，用來「回踩/反彈後站回」的
  進場訊號做完整篩選。
- 這支腳本：由GitHub Actions的伺服器主動、無人值守地定期執行，只做一件事：
  檢查每個交易對的 1D／4H／1H／15m 四個週期，是否「方向完全一致」
  （四個都是多頭，或四個都是空頭），一致才會推播 Telegram 通知。

⚠️ 這裡的「訊號」定義跟 Streamlit App 裡的「回踩站回」進場訊號不同，是更單純
的「多週期方向完全一致」條件，門檻較高但也更嚴格／更少雜訊，適合當作背景
監控用的粗篩通知，實際進場判斷仍建議搭配App做更完整的訊號確認。

為了避免同一個幣種在持續一致的狀態下，每15分鐘就轟炸你一次通知，這支腳本
會把「上一次的一致方向」記錄在 alert_state.json 裡，只有在狀態「剛發生改變」
時（例如從不一致→一致、或從多頭一致→空頭一致）才會真的推播訊息。

環境變數（由 GitHub Actions Secrets 提供，不要寫死在程式碼裡）：
- TELEGRAM_BOT_TOKEN：你的 Telegram Bot Token
- TELEGRAM_CHAT_ID：要推播到的目標 chat id（用 get_telegram_chat_id.py 取得）
"""

import os
import re
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
import pandas as pd
import numpy as np

# ============================================================
# 掃描母體設定（與 Streamlit App 的「掃描母體設定」保持一致，如需調整請同步修改兩邊）
# ============================================================
TOP_N = 200
MIN_QUOTE_VOLUME_USDT = 100 * 10_000  # 最低24小時成交額門檻（萬轉換成USDT）
EXCLUDE_STABLECOINS = True
EXCLUDE_NON_CRYPTO = True

# ============================================================
# 維加斯通道參數（與 Streamlit App 的「維加斯通道參數」保持一致）
# ============================================================
SLOPE_LOOKBACK = 10
MIN_BARS_REQUIRED = 2000
LOOKBACK_BARS = 2000

FUTURES_BASE = "https://api.mexc.com"
STATE_FILE = Path(__file__).resolve().parent / "alert_state.json"

TIMEFRAME_ORDER = ["1D", "4H", "1H", "15M"]
TIMEFRAME_CONFIG = {
    "1D":  {"label": "日線 (1D)",    "interval": "Day1",  "seconds": 86400},
    "4H":  {"label": "4小時 (4H)",   "interval": "Hour4", "seconds": 14400},
    "1H":  {"label": "1小時 (1H)",   "interval": "Min60", "seconds": 3600},
    "15M": {"label": "15分鐘 (15M)", "interval": "Min15", "seconds": 900},
}

STABLECOIN_BASES = {
    "USDC", "TUSD", "DAI", "FDUSD", "USDP", "PYUSD", "EURC",
    "USDD", "GUSD", "BUSD", "UST", "USTC", "USDE", "PAX",
}
NON_CRYPTO_BASE_KEYWORDS = {
    "XAU", "XAG", "XPT", "XPD", "SILVER", "GOLD", "USOIL", "UKOIL", "BRENT", "WTI",
    "COPPER", "NATGAS", "SPX500", "NAS100", "US30", "US100", "US500", "UK100",
    "GER40", "GER30", "JPN225", "FRA40", "AUS200", "HK50", "EU50", "DJI", "DXY",
}
NON_CRYPTO_SUFFIX_RE = re.compile(r"STOCK$")

_SESSION = requests.Session()

_rate_lock_ok = True
import threading
_rate_lock = threading.Lock()
_request_timestamps = []
RATE_CEILING = 9.0
RATE_FLOOR = 3.0
_current_rate = RATE_CEILING
_cooldown_until = 0.0


def _throttle():
    global _current_rate
    with _rate_lock:
        now = time.time()
        while _request_timestamps and now - _request_timestamps[0] > 1.0:
            _request_timestamps.pop(0)
        if len(_request_timestamps) >= _current_rate:
            sleep_time = 1.0 - (now - _request_timestamps[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        _request_timestamps.append(time.time())


def _report_rate_limited():
    global _current_rate, _cooldown_until
    with _rate_lock:
        _current_rate = max(RATE_FLOOR, _current_rate * 0.6)
        _cooldown_until = time.time() + 5.0


def _report_success():
    global _current_rate
    with _rate_lock:
        if time.time() > _cooldown_until and _current_rate < RATE_CEILING:
            _current_rate = min(RATE_CEILING, _current_rate + 0.05)


def fetch_futures_klines_raw(symbol: str, interval: str, target_bars: int, interval_seconds: int,
                              max_retries: int = 5):
    target_bars = min(target_bars, 2000)
    now = int(time.time())
    start = max(0, now - target_bars * interval_seconds - interval_seconds)
    params = {"interval": interval, "start": start}
    for attempt in range(max_retries):
        _throttle()
        try:
            resp = _SESSION.get(f"{FUTURES_BASE}/api/v1/contract/kline/{symbol}", params=params, timeout=15)
        except Exception:
            time.sleep(0.5 + attempt * 0.4)
            continue
        if resp.status_code == 429:
            _report_rate_limited()
            time.sleep(1.2 + attempt * 0.8)
            continue
        if resp.status_code != 200:
            time.sleep(0.5 + attempt * 0.4)
            continue
        try:
            payload = resp.json()
        except Exception:
            time.sleep(0.5 + attempt * 0.4)
            continue
        if not payload.get("success"):
            return None
        _report_success()
        data = payload.get("data")
        if not data or not data.get("time"):
            return None
        try:
            df = pd.DataFrame({
                "OpenTime": pd.to_datetime(data["time"], unit="s"),
                "Open": data["open"], "High": data["high"], "Low": data["low"],
                "Close": data["close"], "Volume": data["vol"],
            })
        except Exception:
            return None
        df = df.drop_duplicates(subset="OpenTime").set_index("OpenTime").sort_index()
        for col in ("Open", "High", "Low", "Close", "Volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(how="all")
    return None


def get_symbol_meta():
    symbols, name_map = [], {}
    try:
        resp = _SESSION.get(f"{FUTURES_BASE}/api/v1/contract/detail", timeout=15)
        if resp.status_code != 200:
            return [], {}, True
        payload = resp.json()
        data = payload.get("data")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return [], {}, True
        for item in data:
            try:
                if item.get("quoteCoin") != "USDT":
                    continue
                sym = item["symbol"]
                base = item.get("baseCoin", sym.split("_")[0])
                symbols.append(sym)
                name_map[sym] = base
            except Exception:
                continue
    except Exception:
        return [], {}, True
    return symbols, name_map, not symbols


def get_ticker_snapshot():
    out = {}
    try:
        resp = _SESSION.get(f"{FUTURES_BASE}/api/v1/contract/ticker", timeout=15)
        if resp.status_code == 200:
            payload = resp.json()
            data = payload.get("data")
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
                for item in data:
                    try:
                        sym = item["symbol"]
                        out[sym] = {
                            "amount24": float(item.get("amount24") or 0),
                            "last_price": float(item.get("lastPrice") or 0),
                        }
                    except Exception:
                        continue
    except Exception:
        pass
    return out


def build_universe(all_symbols, name_map, ticker_snapshot, top_n, min_quote_volume,
                    exclude_stablecoins, exclude_non_crypto):
    rows = []
    for sym in all_symbols:
        snap = ticker_snapshot.get(sym)
        if snap is None or snap["amount24"] < min_quote_volume:
            continue
        base = name_map.get(sym, sym.split("_")[0]).upper()
        if exclude_stablecoins and base in STABLECOIN_BASES:
            continue
        if exclude_non_crypto and (base in NON_CRYPTO_BASE_KEYWORDS or NON_CRYPTO_SUFFIX_RE.search(base)):
            continue
        rows.append((sym, snap["amount24"]))
    rows.sort(key=lambda r: r[1], reverse=True)
    return [r[0] for r in rows[:top_n]]


def compute_ema_only(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_index().copy()
    for span in (144, 169, 576, 676):
        df[f"EMA{span}"] = df["Close"].ewm(span=span, adjust=False).mean()
    return df


def classify_tunnel_status(df: pd.DataFrame, slope_lookback: int) -> str:
    n = len(df)
    if n < slope_lookback + 2:
        return "na"
    last = df.iloc[-1]
    cols = ["EMA144", "EMA169", "EMA576", "EMA676", "Close"]
    if last[cols].isna().any():
        return "na"
    prev = df.iloc[-1 - slope_lookback]
    if prev[["EMA144", "EMA169", "EMA576", "EMA676"]].isna().any():
        return "na"

    small_up = max(last["EMA144"], last["EMA169"])
    small_low = min(last["EMA144"], last["EMA169"])
    small_mid = (last["EMA144"] + last["EMA169"]) / 2
    large_mid = (last["EMA576"] + last["EMA676"]) / 2
    price = last["Close"]

    all_up = all(last[c] > prev[c] for c in ("EMA144", "EMA169", "EMA576", "EMA676"))
    all_down = all(last[c] < prev[c] for c in ("EMA144", "EMA169", "EMA576", "EMA676"))

    if price > small_up and small_mid > large_mid and all_up:
        return "bull"
    if price < small_low and small_mid < large_mid and all_down:
        return "bear"
    return "range"


def fetch_all_timeframes(symbol):
    """回傳 (symbol, {tf: status}, last_close)。任何一個週期資料不足就整組視為無法判斷。"""
    statuses = {}
    last_close = None
    for tf in TIMEFRAME_ORDER:
        cfg = TIMEFRAME_CONFIG[tf]
        raw = fetch_futures_klines_raw(symbol, cfg["interval"], LOOKBACK_BARS, cfg["seconds"])
        if raw is None or len(raw) < MIN_BARS_REQUIRED:
            return symbol, None, None
        df = compute_ema_only(raw)
        statuses[tf] = classify_tunnel_status(df, SLOPE_LOOKBACK)
        if tf == "15M":
            last_close = float(df["Close"].iloc[-1])
    return symbol, statuses, last_close


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15)
        if not resp.ok:
            print(f"Telegram送出失敗: {resp.status_code} {resp.text}")
        return resp.ok
    except Exception as e:
        print(f"Telegram送出例外: {e}")
        return False


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 環境變數，中止執行。")
        sys.exit(1)

    t_start = time.time()
    all_symbols, name_map, is_fallback = get_symbol_meta()
    if is_fallback or not all_symbols:
        print("⚠️ 無法從 MEXC 取得合約清單，本次略過。")
        return
    ticker_snapshot = get_ticker_snapshot()
    universe = build_universe(all_symbols, name_map, ticker_snapshot, TOP_N, MIN_QUOTE_VOLUME_USDT,
                               EXCLUDE_STABLECOINS, EXCLUDE_NON_CRYPTO)
    print(f"母體共 {len(all_symbols)} 個USDT本位永續合約，篩選後鎖定 {len(universe)} 個進行檢查。")

    old_state = load_state()
    new_state = {}
    alerts = []
    checked, skipped = 0, 0

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_all_timeframes, sym) for sym in universe]
        for fut in futures:
            symbol, statuses, last_close = fut.result()
            checked += 1
            if statuses is None:
                skipped += 1
                continue

            aligned = None
            if all(statuses[tf] == "bull" for tf in TIMEFRAME_ORDER):
                aligned = "bull"
            elif all(statuses[tf] == "bear" for tf in TIMEFRAME_ORDER):
                aligned = "bear"

            if aligned is not None:
                new_state[symbol] = aligned
                prev = old_state.get(symbol)
                if prev != aligned:
                    base = name_map.get(symbol, symbol.split("_")[0])
                    dir_label = "🟢 全週期多頭一致" if aligned == "bull" else "🔴 全週期空頭一致"
                    price_str = f"{last_close:,.6f}" if last_close is not None else "—"
                    msg = (
                        f"*{dir_label}*\n"
                        f"合約：`{symbol}` ({base})\n"
                        f"現價：{price_str} USDT\n"
                        f"1D／4H／1H／15M 通道狀態皆為{'多頭' if aligned == 'bull' else '空頭'}\n"
                        f"⚠️ 純技術面多週期一致通知，非投資建議，請自行確認風險"
                    )
                    alerts.append(msg)

    print(f"檢查完成：共檢查 {checked} 個合約，{skipped} 個因資料不足被跳過，"
          f"{len(new_state)} 個目前處於多週期一致狀態，其中 {len(alerts)} 個是新產生的一致狀態。")

    for msg in alerts:
        send_telegram_message(token, chat_id, msg)
        time.sleep(0.5)

    save_state(new_state)
    print(f"耗時 {round(time.time() - t_start, 1)} 秒。")


if __name__ == "__main__":
    main()
