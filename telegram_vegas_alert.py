#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_vegas_alert.py
===================================================================
維加斯通道 - 多週期一致 + 進場訊號 Telegram 通知腳本

這是一支「獨立於 Streamlit App」運作的腳本，設計給 GitHub Actions 排程呼叫
（建議每15分鐘一次，對齊每個整點的0/15/30/45分），跟你平常用瀏覽器打開的
Streamlit互動掃描App是兩個完全分開的東西，但判斷邏輯是完全一致的：

推播通知需要同時滿足兩個條件：
1. 【四週期一致】1D／4H／1H／15m 四個週期的維加斯通道狀態必須完全一致
   （全部是多頭、或全部是空頭），不一致就直接跳過，不檢查下一步。
2. 【真正的進場訊號】在四週期一致的前提下，1D／4H／1H／15m 之中至少要有一個
   週期，出現了跟 Streamlit App 完全相同定義的「回踩/反彈後站回」進場事件
   （價格曾經測試到小通道、目前站回小通道與EMA12之上/之下，且期間未出現
   確認跌破/突破）。只有「方向一致」但沒有任何週期出現真正進場事件，不會
   推播通知。

為了避免同一根尚未走完的K棒在每15分鐘檢查時被重複通知，這支腳本會把「每個
交易對/方向/週期最後一次通知的訊號K棒時間」記錄在 alert_state.json 裡，
只有偵測到「新的一根K棒」觸發訊號時才會真的推播訊息；同一根K棒在收線之前
被重複偵測到，不會重複通知。

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
PULLBACK_LOOKBACK = 3
SCAN_BARS = 1
ATR_MULTIPLIER = 1.5
REQUIRE_VOLUME_CONFIRM = True
VOL_RATIO_MIN = 1.5

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


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_index().copy()
    df["Vol_MA5"] = df["Volume"].rolling(5).mean()

    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14).mean()

    for span in (12, 144, 169, 576, 676):
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


def fmt_price(x):
    if x is None or pd.isna(x):
        return "—"
    ax = abs(float(x))
    if ax == 0:
        return "0"
    if ax >= 1:
        return f"{x:,.4f}"
    elif ax >= 0.01:
        return f"{x:,.6f}"
    else:
        return f"{x:,.8f}"


def strat_vegas_entry_signal(df: pd.DataFrame, direction: str):
    """完整移植自 Streamlit App 的維加斯通道「回踩/反彈後站回」進場訊號判斷。
    回傳 None，或 dict：{"signal_time", "close", "v_ratio", "stop_ref", "note"}。"""
    n = len(df)
    if n < MIN_BARS_REQUIRED:
        return None
    if df[["EMA144", "EMA169", "EMA576", "EMA676", "EMA12"]].iloc[-1].isna().any():
        return None

    small_up = df[["EMA144", "EMA169"]].max(axis=1)
    small_low = df[["EMA144", "EMA169"]].min(axis=1)
    small_mid = df[["EMA144", "EMA169"]].mean(axis=1)
    large_mid = df[["EMA576", "EMA676"]].mean(axis=1)

    slope_n = SLOPE_LOOKBACK

    def is_trending(idx):
        if idx - slope_n < 0:
            return False
        if pd.isna(small_mid.iloc[idx]) or pd.isna(large_mid.iloc[idx]):
            return False
        if direction == "long" and not (small_mid.iloc[idx] > large_mid.iloc[idx]):
            return False
        if direction == "short" and not (small_mid.iloc[idx] < large_mid.iloc[idx]):
            return False
        for col in ("EMA144", "EMA169", "EMA576", "EMA676"):
            prev_v = df[col].iloc[idx - slope_n]
            cur_v = df[col].iloc[idx]
            if pd.isna(prev_v) or pd.isna(cur_v):
                return False
            if direction == "long" and cur_v <= prev_v:
                return False
            if direction == "short" and cur_v >= prev_v:
                return False
        return True

    min_i = PULLBACK_LOOKBACK + slope_n + 1
    for i in range(n - SCAN_BARS, n):
        if i < min_i:
            continue
        row = df.iloc[i]
        if pd.isna(row["EMA12"]) or pd.isna(small_up.iloc[i]) or pd.isna(small_low.iloc[i]):
            continue

        if direction == "long":
            if not (row["Close"] > small_up.iloc[i] and row["Close"] > row["EMA12"]):
                continue
        else:
            if not (row["Close"] < small_low.iloc[i] and row["Close"] < row["EMA12"]):
                continue

        if not is_trending(i - 1):
            continue

        lo = max(0, i - PULLBACK_LOOKBACK)
        window_high = df["High"].iloc[lo:i]
        window_low = df["Low"].iloc[lo:i]
        window_close = df["Close"].iloc[lo:i]
        window_small_up = small_up.iloc[lo:i]
        window_small_low = small_low.iloc[lo:i]
        window_ema12 = df["EMA12"].iloc[lo:i]
        if window_high.empty:
            continue

        if direction == "long":
            touched = (window_low <= window_small_up).any()
        else:
            touched = (window_high >= window_small_low).any()
        if not touched:
            continue

        if direction == "long":
            confirmed_break = ((window_close < window_small_low) & (window_close < window_ema12)).any()
        else:
            confirmed_break = ((window_close > window_small_up) & (window_close > window_ema12)).any()
        if confirmed_break:
            continue

        v_ratio = row["Volume"] / row["Vol_MA5"] if row["Vol_MA5"] > 0 else 1.0
        if REQUIRE_VOLUME_CONFIRM and v_ratio < VOL_RATIO_MIN:
            continue

        atr = df["ATR14"].iloc[-1]
        close_now = float(row["Close"])
        if pd.notna(atr):
            stop_ref = close_now - ATR_MULTIPLIER * float(atr) if direction == "long" \
                else close_now + ATR_MULTIPLIER * float(atr)
        else:
            stop_ref = None

        action = "回踩後站回小通道" if direction == "long" else "反彈後再度跌落小通道"
        note = f"{action}，前{PULLBACK_LOOKBACK}根內測試過小通道，量能放大{round(v_ratio,2)}倍"
        return {
            "signal_time": row.name,
            "close": close_now,
            "v_ratio": v_ratio,
            "stop_ref": stop_ref,
            "note": note,
        }
    return None


def fetch_all_timeframes(symbol):
    """回傳 (symbol, {tf: df}或None, {tf: status}或None, fail_reason, fail_tf)。
    fail_reason: None(成功) / "fetch_fail"(抓取失敗，可能是網路或限速問題) /
    "insufficient_bars"(有抓到資料但根數不足，通常是該合約上市時間不夠長，屬正常情況)。"""
    dfs = {}
    statuses = {}
    for tf in TIMEFRAME_ORDER:
        cfg = TIMEFRAME_CONFIG[tf]
        raw = fetch_futures_klines_raw(symbol, cfg["interval"], LOOKBACK_BARS, cfg["seconds"])
        if raw is None:
            return symbol, None, None, "fetch_fail", tf
        if len(raw) < MIN_BARS_REQUIRED:
            return symbol, None, None, "insufficient_bars", tf
        df = compute_indicators(raw)
        dfs[tf] = df
        statuses[tf] = classify_tunnel_status(df, SLOPE_LOOKBACK)
    return symbol, dfs, statuses, None, None


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
    checked, aligned_count = 0, 0
    fail_reason_counts = {"fetch_fail": 0, "insufficient_bars": 0}
    fail_reason_by_tf = {"fetch_fail": {}, "insufficient_bars": {}}

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_all_timeframes, sym) for sym in universe]
        for fut in futures:
            symbol, dfs, statuses, fail_reason, fail_tf = fut.result()
            checked += 1
            if fail_reason is not None:
                fail_reason_counts[fail_reason] += 1
                fail_reason_by_tf[fail_reason][fail_tf] = fail_reason_by_tf[fail_reason].get(fail_tf, 0) + 1
                continue

            # 條件一：1D/4H/1H/15M 四個週期通道狀態必須完全一致（全多頭或全空頭）
            aligned = None
            if all(statuses[tf] == "bull" for tf in TIMEFRAME_ORDER):
                aligned = "bull"
            elif all(statuses[tf] == "bear" for tf in TIMEFRAME_ORDER):
                aligned = "bear"
            if aligned is None:
                continue
            aligned_count += 1
            direction = "long" if aligned == "bull" else "short"

            # 條件二：在這個一致的環境下，至少一個週期要出現真正的「回踩/反彈後站回」進場訊號
            fired_timeframes = []
            for tf in TIMEFRAME_ORDER:
                hit = strat_vegas_entry_signal(dfs[tf], direction)
                if hit is None:
                    continue
                state_key = f"{symbol}|{direction}|{tf}"
                signal_time_str = str(hit["signal_time"])
                new_state[state_key] = signal_time_str
                # 只有「這根K棒的訊號」跟上次通知過的不是同一根，才真正推播，
                # 避免同一根尚未走完的K棒被15分鐘檢查週期重複通知。
                if old_state.get(state_key) != signal_time_str:
                    fired_timeframes.append((tf, hit))

            if not fired_timeframes:
                continue

            base = name_map.get(symbol, symbol.split("_")[0])
            dir_label = "🟢 做多進場訊號" if direction == "long" else "🔴 做空進場訊號"
            status_line = "／".join(f"{tf}:{'多頭' if statuses[tf]=='bull' else '空頭'}" for tf in TIMEFRAME_ORDER)
            tf_detail_lines = []
            for tf, hit in fired_timeframes:
                stop_str = fmt_price(hit["stop_ref"]) if hit["stop_ref"] is not None else "—"
                tf_detail_lines.append(
                    f"　• {TIMEFRAME_CONFIG[tf]['label']}：{hit['note']}\n"
                    f"　  觸發時間 {hit['signal_time']}　現價 {fmt_price(hit['close'])}　ATR停損參考 {stop_str}"
                )
            msg = (
                f"*{dir_label}*\n"
                f"合約：`{symbol}` ({base})\n"
                f"四週期狀態：{status_line}（已全部一致）\n"
                f"觸發進場條件的週期：\n" + "\n".join(tf_detail_lines) + "\n"
                f"⚠️ 純技術面訊號，非投資建議，請自行確認風險與資金管理"
            )
            alerts.append(msg)

    skipped_total = fail_reason_counts["fetch_fail"] + fail_reason_counts["insufficient_bars"]
    diag_line = (
        f"檢查完成：共檢查 {checked} 個合約，{skipped_total} 個被跳過"
        f"（其中「抓取失敗/可能限速」{fail_reason_counts['fetch_fail']} 個，"
        f"「歷史資料不足2000根」{fail_reason_counts['insufficient_bars']} 個），"
        f"{aligned_count} 個目前四週期方向一致，其中 {len(alerts)} 個出現新的進場訊號。"
    )
    print(diag_line)
    if fail_reason_by_tf["insufficient_bars"]:
        print("「歷史資料不足」依週期分布：", fail_reason_by_tf["insufficient_bars"],
              "（通常1D佔多數是正常的，因為要求2000根日線=2000天歷史，多數合約上市沒那麼久）")
    if fail_reason_by_tf["fetch_fail"]:
        print("「抓取失敗」依週期分布：", fail_reason_by_tf["fetch_fail"],
              "（若這個數字偏高，才比較可能是限速或網路問題）")

    # -------------------------------------------------------------
    # 除錯用「心跳」訊息：本次沒有任何新訊號時，仍發一則簡短通知，
    # 方便確認排程真的有在跑（而不是無聲無息、你也不知道是沒訊號還是整個沒執行）。
    # 之後如果覺得沒訊號也要收到通知太吵，把 SEND_HEARTBEAT_WHEN_NO_SIGNAL 改成 False 即可。
    # -------------------------------------------------------------
    SEND_HEARTBEAT_WHEN_NO_SIGNAL = True
    if not alerts and SEND_HEARTBEAT_WHEN_NO_SIGNAL:
        heartbeat_msg = (
            f"🔕 *當前無交易訊號*\n"
            f"本次掃描時間：{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n"
            f"共檢查 {checked} 個合約，{aligned_count} 個四週期方向一致，但沒有新的進場訊號。\n"
            f"（跳過：抓取失敗 {fail_reason_counts['fetch_fail']} 個／歷史資料不足 {fail_reason_counts['insufficient_bars']} 個）\n"
            f"系統運作正常，此為確認排程有執行的提示訊息。"
        )
        send_telegram_message(token, chat_id, heartbeat_msg)

    for msg in alerts:
        send_telegram_message(token, chat_id, msg)
        time.sleep(0.5)

    save_state(new_state)
    print(f"耗時 {round(time.time() - t_start, 1)} 秒。")


if __name__ == "__main__":
    main()
