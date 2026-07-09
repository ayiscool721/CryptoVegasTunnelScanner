#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
維加斯通道 永續合約掃描雷達 - MEXC Futures 專版
===================================================================
執行指令：streamlit run mexc_futures_vegas_scanner.py

本檔案是「幣圈 CYBER QUANT 選幣雷達」的重新聚焦版本，依照使用者需求大幅簡化：

1. 只保留「維加斯通道 (Vegas Tunnel)」一種策略，移除其餘8種選幣策略。
2. 掃描目標由「現貨 Spot」改為「永續合約 Futures」，資料來源改用 MEXC 合約
   公開 API（https://api.mexc.com/api/v1/contract/...），一樣免金鑰。
3. 每個交易對的掃描結果，會同時顯示 1D／4H／1H／15m 四個K線級別的「通道狀態」
   （多頭／空頭／盤整），做為多週期對照參考欄位。
4. 訊號（進場點）在 1D／4H／1H／15m 四個週期上各自獨立判斷，彼此不互相驗證。
5. 預設同時篩選做多與做空訊號。

── 判斷邏輯（已與使用者確認）──────────────────────────────────
Ａ．單一週期的「通道狀態」分類（僅供顯示參考，不是進場訊號）：
   - 多頭：價格在小通道(EMA144/169)上方，且小通道中心 > 大通道(EMA576/676)中心，
     且四條EMA(144/169/576/676)相較N根K棒前都是上揚。
   - 空頭：價格在小通道下方，且小通道中心 < 大通道中心，且四條EMA都下彎。
   - 其餘情況：盤整。（資料不足以計算時顯示「資料不足」）
   註：大小通道位置關係採「中心點比較」（允許輕微重疊），斜率則要求四條EMA
   同方向變動，這兩點都已與使用者確認。

Ｂ．進場訊號篩選（做多為例，做空為完全鏡射邏輯）：
   1. 大通道與小通道皆為上揚，且小通道中心在大通道中心之上（即該週期通道結構
      已經是「多頭」，見Ａ）。
   2. 前 X 根K棒內，價格曾經碰觸或跌破小通道，但「當根」價格與EMA12同時重新
      站回小通道之上，且期間未曾出現「收盤同時跌破小通道下緣與EMA12」的
      確認跌破（如曾出現則排除，不視為買點）。
   做空訊號為完全鏡射：通道需為空頭結構，前X根內曾反彈觸及/突破小通道，當根
   價格與EMA12同時重新跌回小通道之下，且期間未曾出現確認站上小通道與EMA12。

⚠️ 重要提醒：MEXC永續合約本身即為槓桿商品，做多與做空都可直接下單，但涉及
資金費率、強制平倉、保證金追繳等現貨沒有的風險，請自行了解相關機制後再操作。

免責聲明：本工具僅為技術面資料整理與型態篩選之輔助工具，所有結果均為歷史
數據的機械式判斷，不構成任何投資建議，永續合約槓桿波動與歸零風險極高，
請自行審慎評估風險。
"""

import re
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import streamlit as st

# ============================================================
# 0. 頁面設定 + Cyber 風格 CSS
# ============================================================
st.set_page_config(
    page_title="維加斯通道 永續合約掃描雷達 (MEXC Futures)",
    layout="wide",
    page_icon="🎰",
    initial_sidebar_state="expanded",
)

CYBER_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=JetBrains+Mono:wght@400;500;700&display=swap');

html, body, [class*="css"]  { font-family: 'JetBrains Mono', monospace; }

.stApp {
    background: radial-gradient(circle at 15% -10%, #10243a 0%, #060b14 42%, #020306 100%);
}
.stApp, .stApp p, .stApp span, .stApp label, .stApp li, .stApp .stMarkdown,
.stApp h1, .stApp h2, .stApp h3, .stApp h4 {
    color: #d9f6ff;
}

h1, h2, h3 { font-family: 'Orbitron', sans-serif !important; letter-spacing: 0.5px; }

h1 {
    background: linear-gradient(90deg, #00e5ff, #7dffea 45%, #ff2fd0);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    filter: drop-shadow(0 0 18px rgba(0,229,255,0.35));
}

section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #050a12 0%, #0a1520 100%);
    border-right: 1px solid rgba(0,229,255,0.25);
}
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] li,
section[data-testid="stSidebar"] .stMarkdown {
    color: #b9f5ff;
}
section[data-testid="stSidebar"] hr { border-color: rgba(0,229,255,0.18); }

div[data-baseweb="select"] > div,
div[data-baseweb="input"],
div[data-baseweb="base-input"],
input, textarea {
    background-color: #0b1926 !important;
    color: #eaf9ff !important;
    border-color: rgba(0,229,255,0.35) !important;
}
div[data-baseweb="select"] svg { fill: #eaf9ff !important; }

ul[role="listbox"], div[data-baseweb="popover"] div[data-baseweb="menu"] {
    background-color: #0b1926 !important;
    border: 1px solid rgba(0,229,255,0.3) !important;
}
li[role="option"], div[data-baseweb="menu"] li {
    background-color: #0b1926 !important;
    color: #eaf9ff !important;
}
li[role="option"]:hover, div[data-baseweb="menu"] li:hover {
    background-color: rgba(0,229,255,0.18) !important;
}

button[data-testid="stNumberInputStepUp"],
button[data-testid="stNumberInputStepDown"] {
    background-color: #0b1926 !important;
    color: #eaf9ff !important;
}

div[role="tooltip"] { background-color: #f2fbff !important; }
div[role="tooltip"], div[role="tooltip"] * { color: #0b1420 !important; }

div[data-testid="stAlert"] {
    background-color: rgba(235, 248, 255, 0.96) !important;
    border-radius: 10px;
}
div[data-testid="stAlert"] p,
div[data-testid="stAlert"] span,
div[data-testid="stAlert"] div,
div[data-testid="stAlert"] li {
    color: #0b1420 !important;
}

div[data-testid="stMetric"] {
    background: linear-gradient(160deg, rgba(0,229,255,0.08), rgba(255,47,208,0.04));
    border: 1px solid rgba(0,229,255,0.35);
    border-radius: 12px;
    padding: 10px 16px;
    box-shadow: 0 0 20px rgba(0,229,255,0.10);
}
div[data-testid="stMetricValue"] { color: #7dffea !important; font-family: 'Orbitron', sans-serif; }
div[data-testid="stMetricLabel"] { color: #8fd7ff !important; }

.stButton>button, .stDownloadButton>button {
    background: linear-gradient(90deg, #00e5ff, #0083ff);
    color: #04121a !important;
    font-weight: 700;
    border: none;
    border-radius: 8px;
    box-shadow: 0 0 18px rgba(0,229,255,0.45);
    transition: all 0.15s ease-in-out;
}
.stButton>button:hover, .stDownloadButton>button:hover {
    box-shadow: 0 0 28px rgba(0,229,255,0.85);
    transform: translateY(-1px);
}
.stButton>button *, .stDownloadButton>button * { color: #04121a !important; }

div[data-testid="stDataFrame"] {
    border: 1px solid rgba(0,229,255,0.25);
    border-radius: 10px;
    overflow: hidden;
}

div[data-testid="stExpander"] {
    border: 1px solid rgba(0,229,255,0.20) !important;
    border-radius: 8px;
    background: rgba(6,14,22,0.75);
}
div[data-testid="stExpander"] summary,
div[data-testid="stExpander"] summary span,
div[data-testid="stExpander"] summary p {
    color: #d9f6ff !important;
}

div[data-testid="stCheckbox"] label p,
div[data-testid="stSlider"] label p,
div[data-testid="stNumberInput"] label p,
div[data-testid="stSelectbox"] label p {
    color: #d9f6ff !important;
}
div[data-testid="stSlider"] div[data-testid="stTickBar"] { color: #8fd7ff !important; }
div[data-testid="stSlider"] * { color: #d9f6ff; }

.stTabs [data-baseweb="tab"] { font-family: 'Orbitron', sans-serif; color: #8fd7ff; }
.stTabs [aria-selected="true"] { color: #00e5ff !important; border-bottom: 2px solid #00e5ff !important; }

.pill {
    display: inline-block; padding: 4px 16px; border-radius: 20px;
    font-family: 'Orbitron', sans-serif; font-size: 0.85rem; font-weight: 700;
    letter-spacing: 1px; border: 1px solid;
}
.pill-bull  { color:#00ff8c; border-color:#00ff8c; background:rgba(0,255,140,0.10); box-shadow:0 0 14px rgba(0,255,140,0.45); }
.pill-bear  { color:#ff4d6d; border-color:#ff4d6d; background:rgba(255,77,109,0.10); box-shadow:0 0 14px rgba(255,77,109,0.45); }
.pill-flat  { color:#c9c9c9; border-color:#8a8a8a; background:rgba(255,255,255,0.05); }

.cyber-card {
    border: 1px solid rgba(0,229,255,0.25);
    border-radius: 12px;
    padding: 14px 18px;
    background: rgba(0,229,255,0.03);
    margin-bottom: 10px;
}

.tv-link-wrap {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 6px 0 14px 0;
}
.tv-link-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 14px;
    border-radius: 20px;
    text-decoration: none !important;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    font-weight: 600;
    border: 1px solid rgba(0,229,255,0.4);
    background: rgba(0,229,255,0.06);
    color: #7dffea !important;
    transition: all 0.15s ease-in-out;
}
.tv-link-pill:hover {
    background: rgba(0,229,255,0.18);
    box-shadow: 0 0 14px rgba(0,229,255,0.4);
}
.tv-link-pill.dir-long { border-color: rgba(0,255,140,0.5); color: #00ff8c !important; }
.tv-link-pill.dir-short { border-color: rgba(255,77,109,0.5); color: #ff4d6d !important; }
</style>
"""
st.markdown(CYBER_CSS, unsafe_allow_html=True)

st.markdown("# 🎰 維加斯通道 永續合約掃描雷達")
st.caption("MEXC 永續合約(Futures)・維加斯通道多空雙向訊號・1D/4H/1H/15m 多週期通道狀態對照")
st.info("⚠️ 永續合約為槓桿商品，涉及資金費率、強制平倉、保證金追繳等風險，"
        "本工具訊號僅為技術面判讀，不構成投資建議。", icon="⚠️")

FUTURES_BASE = "https://api.mexc.com"
_SESSION = requests.Session()

# ============================================================
# 0.5 K線級別設定（MEXC 合約原生週期字串 + 秒數，用於換算回溯起始時間）
# ============================================================
TIMEFRAME_ORDER = ["1D", "4H", "1H", "15M"]
TIMEFRAME_CONFIG = {
    "1D":  {"label": "日線 (1D)",    "interval": "Day1",  "seconds": 86400},
    "4H":  {"label": "4小時 (4H)",   "interval": "Hour4", "seconds": 14400},
    "1H":  {"label": "1小時 (1H)",   "interval": "Min60", "seconds": 3600},
    "15M": {"label": "15分鐘 (15M)", "interval": "Min15", "seconds": 900},
}

STATUS_LABELS = {"bull": "🟢多頭", "bear": "🔴空頭", "range": "⚪盤整", "na": "⚫資料不足", "off": "—未掃描"}

STABLECOIN_BASES = {
    "USDC", "TUSD", "DAI", "FDUSD", "USDP", "PYUSD", "EURC",
    "USDD", "GUSD", "BUSD", "UST", "USTC", "USDE", "PAX",
}
# MEXC合約也提供股票/商品/指數CFD（例如 XAU_USDT、SPX500_USDT、AAPLSTOCK_USDT），
# 這些不是加密貨幣本身，以下為盡力而為的關鍵字過濾清單（無法保證100%完整）。
NON_CRYPTO_BASE_KEYWORDS = {
    "XAU", "XAG", "XPT", "XPD", "SILVER", "GOLD", "USOIL", "UKOIL", "BRENT", "WTI",
    "COPPER", "NATGAS", "SPX500", "NAS100", "US30", "US100", "US500", "UK100",
    "GER40", "GER30", "JPN225", "FRA40", "AUS200", "HK50", "EU50", "DJI", "DXY",
}
NON_CRYPTO_SUFFIX_RE = re.compile(r"STOCK$")


def fmt_price(x):
    """價格跨越多個數量級，依大小動態決定小數位數"""
    if x is None:
        return np.nan
    try:
        if pd.isna(x):
            return np.nan
    except (TypeError, ValueError):
        pass
    ax = abs(float(x))
    if ax == 0:
        return 0.0
    if ax >= 1:
        return round(float(x), 4)
    elif ax >= 0.01:
        return round(float(x), 6)
    else:
        return round(float(x), 8)


def fmt_millions(x):
    if x is None or pd.isna(x):
        return np.nan
    return round(float(x) / 1_000_000, 2)


def tradingview_url(futures_symbol: str) -> str:
    """把 MEXC合約格式(如 BTC_USDT) 轉成 TradingView 永續合約圖表連結。
    TradingView上MEXC永續合約的代碼格式為 MEXC:BTCUSDT.P（無底線、結尾加.P）。"""
    tv_symbol = futures_symbol.replace("_", "") + ".P"
    return f"https://www.tradingview.com/chart/?symbol=MEXC%3A{tv_symbol}"


def render_tv_link_list(df: pd.DataFrame) -> str:
    """把交易對清單渲染成一組真正的 HTML <a> 超連結（不是 st.dataframe 內建的 LinkColumn）。
    st.dataframe 的表格是用 canvas 繪製、連結點擊是透過 JavaScript window.open() 觸發，
    手機瀏覽器對這種「非使用者直接點擊錨點」的導覽通常不會觸發 iOS/Android 的
    Universal Link／App Link機制，所以無法自動改用TradingView App開啟。
    改用真正的 <a href="..."> 標籤，才能讓手機作業系統正確接手、優先用已安裝的App開啟。"""
    seen = set()
    items = []
    for _, row in df.iterrows():
        sym = row.get("交易對")
        if sym is None:
            continue
        direction = row.get("方向", "")
        tf = row.get("訊號週期", "")
        dedup_key = (sym, direction, tf)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        dir_class = "dir-long" if "做多" in str(direction) else ("dir-short" if "做空" in str(direction) else "")
        name = row.get("幣種名稱", sym)
        label = f"{name} {direction} {tf}".strip()
        url = tradingview_url(sym)
        items.append(f'<a class="tv-link-pill {dir_class}" href="{url}" target="_blank" '
                     f'rel="noopener noreferrer">📈 {label}</a>')
    if not items:
        return '<div class="tv-link-wrap"><span style="color:#8fd7ff;">（無資料）</span></div>'
    return '<div class="tv-link-wrap">' + "".join(items) + "</div>"


# ============================================================
# 1. 資料抓取層：MEXC 合約(Futures) 公開 API
# ============================================================
@st.cache_data(ttl=3600)
def get_futures_symbol_meta():
    """回傳 (symbols, name_map, is_fallback)。symbols 僅含 USDT本位永續合約，
    name_map: symbol -> 幣種基礎資產代號 (baseCoin)"""
    symbols, name_map = [], {}
    is_fallback = False
    try:
        resp = _SESSION.get(f"{FUTURES_BASE}/api/v1/contract/detail", timeout=10)
        if resp.status_code == 200:
            payload = resp.json()
            data = payload.get("data")
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
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
            else:
                is_fallback = True
        else:
            is_fallback = True
    except Exception:
        is_fallback = True

    if not symbols:
        is_fallback = True
    return symbols, name_map, is_fallback


@st.cache_data(ttl=120)
def get_futures_ticker_snapshot():
    """回傳 dict：symbol -> {amount24, volume24, rise_fall_pct, funding_rate, last_price}"""
    out = {}
    try:
        resp = _SESSION.get(f"{FUTURES_BASE}/api/v1/contract/ticker", timeout=10)
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
                            "volume24": float(item.get("volume24") or 0),
                            "rise_fall_pct": float(item.get("riseFallRate") or 0) * 100,
                            "funding_rate_pct": float(item.get("fundingRate") or 0) * 100,
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
        if snap is None:
            continue
        if snap["amount24"] < min_quote_volume:
            continue
        base = name_map.get(sym, sym.split("_")[0]).upper()
        if exclude_stablecoins and base in STABLECOIN_BASES:
            continue
        if exclude_non_crypto and (base in NON_CRYPTO_BASE_KEYWORDS or NON_CRYPTO_SUFFIX_RE.search(base)):
            continue
        rows.append((sym, snap["amount24"]))
    rows.sort(key=lambda r: r[1], reverse=True)
    return [r[0] for r in rows[:top_n]]


import threading

_rate_lock = threading.Lock()
_request_timestamps = []
# MEXC合約K線端點官方文件標示的限速約為每2秒20次（約10次/秒）。
# 採用「自適應」節流：平時貼著上限跑（9次/秒），一旦真的遇到429就立刻降速並暫時冷卻，
# 冷卻期過後再逐步回升，而不是永遠固定用保守值卡住速度。
RATE_CEILING = 9.0
RATE_FLOOR = 3.0
_current_rate = RATE_CEILING
_cooldown_until = 0.0


def _throttle():
    """跨執行緒共用的自適應速率限制器。"""
    with _rate_lock:
        now = time.time()
        while _request_timestamps and now - _request_timestamps[0] > 1.0:
            _request_timestamps.pop(0)
        effective_rate = _current_rate
        if len(_request_timestamps) >= effective_rate:
            sleep_time = 1.0 - (now - _request_timestamps[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        _request_timestamps.append(time.time())


def _report_rate_limited():
    """遇到429時呼叫：立即降速並進入冷卻期，冷卻期滿後才允許速度回升。"""
    global _current_rate, _cooldown_until
    with _rate_lock:
        _current_rate = max(RATE_FLOOR, _current_rate * 0.6)
        _cooldown_until = time.time() + 5.0


def _report_success():
    """請求成功時呼叫：冷卻期過後緩慢回升速度，逐步逼近上限。"""
    global _current_rate
    with _rate_lock:
        if time.time() > _cooldown_until and _current_rate < RATE_CEILING:
            _current_rate = min(RATE_CEILING, _current_rate + 0.05)


def fetch_futures_klines_raw(symbol: str, interval: str, target_bars: int, interval_seconds: int,
                              max_retries: int = 5):
    """MEXC合約K線單次最多回傳2000根，無需分頁；用 start 參數換算回溯起始時間。
    內建自適應速率限制與重試機制：即使遇到429限速或短暫網路錯誤也會重試，避免把
    「抓取失敗」誤判成「資料不足」。"""
    target_bars = min(target_bars, 2000)
    now = int(time.time())
    start = max(0, now - target_bars * interval_seconds - interval_seconds)
    params = {"interval": interval, "start": start}
    last_error = None
    for attempt in range(max_retries):
        _throttle()
        try:
            resp = _SESSION.get(f"{FUTURES_BASE}/api/v1/contract/kline/{symbol}", params=params, timeout=15)
        except Exception as e:
            last_error = e
            time.sleep(0.5 + attempt * 0.4)
            continue
        if resp.status_code == 429:
            _report_rate_limited()
            time.sleep(1.2 + attempt * 0.8)
            continue
        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code}"
            time.sleep(0.5 + attempt * 0.4)
            continue
        try:
            payload = resp.json()
        except Exception as e:
            last_error = e
            time.sleep(0.5 + attempt * 0.4)
            continue
        if not payload.get("success"):
            # API本身回應成功但業務邏輯失敗（例如合約已下市），不需要重試
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
        df = df.dropna(how="all")
        return df
    return None


def _download_raw_batch(symbols: list, interval: str, target_bars: int, interval_seconds: int,
                         max_workers: int = 6) -> dict:
    all_data = {}
    fail_count = 0

    def _job(sym):
        try:
            df = fetch_futures_klines_raw(sym, interval, target_bars, interval_seconds)
            if df is not None and not df.empty:
                return sym, df
        except Exception:
            pass
        return sym, None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_job, s) for s in symbols]
        for fut in futures:
            sym, df = fut.result()
            if df is not None:
                all_data[sym] = df
            else:
                fail_count += 1
    all_data["__fail_count__"] = fail_count
    return all_data


@st.cache_data(ttl=14400)
def _download_batch_cached_1d(symbols: tuple, interval: str, target_bars: int, interval_seconds: int) -> dict:
    return _download_raw_batch(list(symbols), interval, target_bars, interval_seconds)


@st.cache_data(ttl=3600)
def _download_batch_cached_4h(symbols: tuple, interval: str, target_bars: int, interval_seconds: int) -> dict:
    return _download_raw_batch(list(symbols), interval, target_bars, interval_seconds)


@st.cache_data(ttl=900)
def _download_batch_cached_1h(symbols: tuple, interval: str, target_bars: int, interval_seconds: int) -> dict:
    return _download_raw_batch(list(symbols), interval, target_bars, interval_seconds)


@st.cache_data(ttl=180)
def _download_batch_cached_15m(symbols: tuple, interval: str, target_bars: int, interval_seconds: int) -> dict:
    return _download_raw_batch(list(symbols), interval, target_bars, interval_seconds)


_BATCH_CACHE_FUNCS = {
    "1D": _download_batch_cached_1d,
    "4H": _download_batch_cached_4h,
    "1H": _download_batch_cached_1h,
    "15M": _download_batch_cached_15m,
}


def download_klines_batch(symbols: list, timeframe_key: str, target_bars: int) -> dict:
    cfg = TIMEFRAME_CONFIG[timeframe_key]
    sym_tuple = tuple(symbols)
    return _BATCH_CACHE_FUNCS[timeframe_key](sym_tuple, cfg["interval"], target_bars, cfg["seconds"])


# ============================================================
# 2. 技術指標計算（僅計算維加斯通道所需欄位）
# ============================================================
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


# ============================================================
# 3. 通道狀態分類（僅供顯示參考的 1D/4H/1H/15m 多空狀態欄位）
# ============================================================
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


# ============================================================
# 4. 維加斯通道進場訊號（做多／做空鏡射邏輯，單一週期獨立判斷）
# ============================================================
def build_hit(df, symbol, direction, tf_key, signal_date, v_ratio, note, atr_multiplier, status_snapshot):
    if hasattr(signal_date, "hour") and (signal_date.hour != 0 or signal_date.minute != 0):
        sig_date_str = signal_date.strftime("%Y-%m-%d %H:%M")
    elif hasattr(signal_date, "strftime"):
        sig_date_str = signal_date.strftime("%Y-%m-%d")
    else:
        sig_date_str = str(signal_date)

    atr = df["ATR14"].iloc[-1]
    close_now = float(df["Close"].iloc[-1])
    if pd.notna(atr):
        stop_ref = fmt_price(close_now - atr_multiplier * float(atr)) if direction == "long" \
            else fmt_price(close_now + atr_multiplier * float(atr))
    else:
        stop_ref = np.nan

    return {
        "交易對": symbol,
        "方向": "🟢 做多" if direction == "long" else "🔴 做空",
        "訊號週期": TIMEFRAME_CONFIG[tf_key]["label"],
        "訊號觸發時間": sig_date_str,
        "當前收盤價": fmt_price(close_now),
        "當根K棒成交量": round(float(df["Volume"].iloc[-1]), 2),
        "量能放大倍數": round(float(v_ratio), 2),
        "ATR參考停損價": stop_ref,
        "1D狀態": STATUS_LABELS.get(status_snapshot.get("1D"), "—"),
        "4H狀態": STATUS_LABELS.get(status_snapshot.get("4H"), "—"),
        "1H狀態": STATUS_LABELS.get(status_snapshot.get("1H"), "—"),
        "15M狀態": STATUS_LABELS.get(status_snapshot.get("15M"), "—"),
        "備註說明": note,
    }


def strat_vegas_signal(df, symbol, direction, tf_key, p, status_snapshot):
    n = len(df)
    if n < p["min_bars_required"]:
        return None
    if df[["EMA144", "EMA169", "EMA576", "EMA676", "EMA12"]].iloc[-1].isna().any():
        return None

    small_up = df[["EMA144", "EMA169"]].max(axis=1)
    small_low = df[["EMA144", "EMA169"]].min(axis=1)
    small_mid = df[["EMA144", "EMA169"]].mean(axis=1)
    large_mid = df[["EMA576", "EMA676"]].mean(axis=1)

    slope_n = p["slope_lookback"]

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

    min_i = p["pullback_lookback"] + slope_n + 1
    for i in range(n - p["scan_bars"], n):
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

        lo = max(0, i - p["pullback_lookback"])
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
        if p["require_volume_confirm"] and v_ratio < p["vol_ratio_min"]:
            continue

        action = "回踩後站回小通道" if direction == "long" else "反彈後再度跌落小通道"
        tunnel_state = "多頭" if direction == "long" else "空頭"
        note = f"{action}，大通道與小通道皆維持{tunnel_state}結構；前{p['pullback_lookback']}根內測試過小通道"
        return build_hit(df, symbol, direction, tf_key, row.name, v_ratio, note, p["atr_multiplier"], status_snapshot)
    return None


def analyze_symbol(symbol, raw_by_tf, p, active_directions, apply_market_filter, btc_1d_status, active_timeframes):
    computed = {}
    status_snapshot = {}
    for tf in TIMEFRAME_ORDER:
        if tf not in active_timeframes:
            status_snapshot[tf] = "off"
            computed[tf] = None
            continue
        raw_df = raw_by_tf.get(tf)
        if raw_df is None or raw_df.empty or len(raw_df) < 30:
            status_snapshot[tf] = "na"
            computed[tf] = None
            continue
        try:
            df = compute_indicators(raw_df)
        except Exception:
            status_snapshot[tf] = "na"
            computed[tf] = None
            continue
        computed[tf] = df
        try:
            status_snapshot[tf] = classify_tunnel_status(df, p["slope_lookback"])
        except Exception:
            status_snapshot[tf] = "na"

    hits = []
    for tf in active_timeframes:
        df = computed.get(tf)
        if df is None or len(df) < p["min_bars_required"]:
            continue
        for direction in active_directions:
            if apply_market_filter:
                if direction == "long" and btc_1d_status != "bull":
                    continue
                if direction == "short" and btc_1d_status != "bear":
                    continue
            try:
                hit = strat_vegas_signal(df, symbol, direction, tf, p, status_snapshot)
            except Exception:
                hit = None
            if hit is not None:
                hits.append(hit)
    return hits


# ============================================================
# 5. 側邊欄：掃描母體設定 + 維加斯通道參數（統一套用於四個K線級別）
# ============================================================
st.sidebar.markdown("## 🌐 掃描母體設定（MEXC USDT本位永續合約）")
top_n = st.sidebar.slider(
    "依24小時成交額取前 N 名", min_value=20, max_value=500, value=200, step=10,
    help="依合約24小時成交額（USDT計價）排序，只掃描前N名，數字越大掃描越完整但也越慢。"
)
min_quote_volume_wan = st.sidebar.number_input(
    "最低24小時成交額門檻 (萬 USDT)", min_value=0.0, value=100.0, step=10.0,
    help="24小時成交額低於此門檻的合約會被排除，用來過濾流動性不足的冷門合約。"
)
exclude_stablecoins = st.sidebar.checkbox(
    "排除穩定幣相關合約 (USDC/DAI等)", value=True,
    help="穩定幣相關合約價格波動極小，技術分析型態通常沒有意義，預設排除。"
)
exclude_non_crypto = st.sidebar.checkbox(
    "排除非純加密貨幣合約 (股票/商品/指數CFD)", value=True,
    help="MEXC合約也提供黃金、原油、美股、指數等CFD商品（例如XAU_USDT、SPX500_USDT、"
         "AAPLSTOCK_USDT），這些不是加密貨幣本身。此為盡力而為的關鍵字過濾，"
         "無法保證涵蓋所有品項，如發現漏網之魚可自行關閉此選項改用「掃描結果」自行排除。"
)
st.sidebar.markdown("---")

st.sidebar.markdown("## ⏱️ 掃描K線級別（取消勾選可直接減少請求數、加快掃描）")
st.sidebar.caption("狀態欄位與訊號都只會用「有勾選」的週期計算；未勾選的週期會顯示「—」。"
                    "MEXC合約K線請求數 = 合約數 × 已勾選週期數，勾越少掃越快。")
tf_enabled = {}
_tf_cols = st.sidebar.columns(4)
for _idx, _tf in enumerate(TIMEFRAME_ORDER):
    with _tf_cols[_idx]:
        tf_enabled[_tf] = st.checkbox(_tf, value=True, key=f"tf_on_{_tf}")
ACTIVE_TIMEFRAMES = [tf for tf in TIMEFRAME_ORDER if tf_enabled[tf]]

st.sidebar.markdown("---")

st.sidebar.markdown("## 🎰 維加斯通道參數（統一套用於已勾選的K線級別）")
lookback_bars_global = st.sidebar.slider(
    "下載歷史資料根數 (K棒根數)", min_value=200, max_value=2000, value=2000, step=50,
    help="MEXC合約K線單次最多可取2000根，已勾選的K線級別都會用這個根數下載。"
         "維加斯通道的EMA576/676至少需要數百根K棒才會收斂，建議維持800根以上。"
)
min_bars_required = st.sidebar.slider(
    "最少所需K棒數", min_value=200, max_value=2000, value=2000, step=50,
    help="資料根數不足此值的週期會直接跳過該週期的訊號判斷（顯示「資料不足」），"
         "避免用尚未收斂的EMA576/676誤判。"
)
slope_lookback = st.sidebar.slider(
    "通道方向判斷回溯根數", min_value=3, max_value=20, value=10, step=1,
    help="判斷EMA144/169/576/676是否『同方向』時，與這個根數之前的數值比較，"
         "數值越大代表判斷越平滑、越不敏感。此設定同時套用於狀態分類與訊號判斷。"
)
pullback_lookback = st.sidebar.slider(
    "回踩/反彈觀察根數 (前X根)", min_value=3, max_value=30, value=3, step=1,
    help="在這段根數內，價格必須曾經測試到小通道（EMA144/169）附近或微幅穿越，才符合型態。"
)
scan_bars_global = st.sidebar.slider(
    "訊號偵測範圍 (最近幾根K棒)", min_value=1, max_value=10, value=1, step=1,
    help="只顯示『最近幾根K棒』內出現的訊號，超過這個範圍就視為過期訊號、不會顯示。"
)
atr_multiplier = st.sidebar.slider(
    "ATR 停損參考倍數", min_value=1.0, max_value=3.0, value=1.5, step=0.1,
    help="做多停損參考價 = 現價 − ATR14 × 此倍數；做空停損參考價 = 現價 + ATR14 × 此倍數。"
         "此數字僅供參考，不是投資建議。"
)
require_volume_confirm = st.sidebar.checkbox(
    "訊號當根需符合量能放大倍數", value=True,
    help="維加斯通道系統的原始定義並不強制要求量能，此為可選的額外過濾條件，"
         "勾選後可提高訊號確認度但數量會變少。"
)
vol_ratio_min = st.sidebar.slider(
    "量能放大倍數 (倍)", min_value=1.0, max_value=3.0, value=1.5, step=0.1,
    help="訊號當根K棒的成交量，需達到當時5根K棒均量的這個倍數以上"
         "（僅在上方勾選『需符合量能放大倍數』時生效）。"
)

st.sidebar.markdown("---")
st.sidebar.markdown("## 🎯 訊號方向")
want_long = st.sidebar.checkbox("🟢 篩選做多訊號", value=True)
want_short = st.sidebar.checkbox("🔴 篩選做空訊號", value=True)

st.sidebar.markdown("---")
st.sidebar.markdown("### 🌍 BTC 大盤環境濾網")
apply_market_filter = st.sidebar.checkbox(
    "依BTC/USDT日線通道狀態篩選訊號", value=False,
    help="勾選後，做多訊號只在 BTC/USDT 日線通道狀態為「多頭」時顯示；"
         "做空訊號只在 BTC/USDT 日線通道狀態為「空頭」時顯示；BTC日線為「盤整」時兩者皆不顯示。"
         "此濾網固定看BTC日線，與上方是否勾選1D週期無關（BTC儀表板獨立抓取）。"
)

vegas_params = {
    "slope_lookback": slope_lookback,
    "pullback_lookback": pullback_lookback,
    "scan_bars": scan_bars_global,
    "atr_multiplier": atr_multiplier,
    "require_volume_confirm": require_volume_confirm,
    "vol_ratio_min": vol_ratio_min,
    "min_bars_required": min_bars_required,
}

active_directions = []
if want_long:
    active_directions.append("long")
if want_short:
    active_directions.append("short")

# ============================================================
# 6. BTC/USDT 永續合約 大盤通道狀態儀表板
# ============================================================
@st.cache_data(ttl=900)
def get_btc_futures_status_all_tf(_slope_lookback: int, target_bars: int):
    result = {}
    for tf in TIMEFRAME_ORDER:
        cfg = TIMEFRAME_CONFIG[tf]
        raw = fetch_futures_klines_raw("BTC_USDT", cfg["interval"], target_bars, cfg["seconds"])
        if raw is None or raw.empty or len(raw) < 30:
            result[tf] = ("na", None)
            continue
        df = compute_indicators(raw)
        status = classify_tunnel_status(df, _slope_lookback)
        result[tf] = (status, df)
    return result

st.markdown("### 🌐 BTC/USDT 永續合約 維加斯通道狀態")
btc_status_data = get_btc_futures_status_all_tf(slope_lookback, lookback_bars_global)
btc_1d_status = btc_status_data.get("1D", ("na", None))[0]

cols = st.columns(4)
pill_class = {"bull": "pill-bull", "bear": "pill-bear", "range": "pill-flat", "na": "pill-flat"}
for idx, tf in enumerate(TIMEFRAME_ORDER):
    status, df_tf = btc_status_data.get(tf, ("na", None))
    with cols[idx]:
        st.markdown(f"**{TIMEFRAME_CONFIG[tf]['label']}**")
        st.markdown(f'<span class="pill {pill_class.get(status,"pill-flat")}">{STATUS_LABELS.get(status,"⚫資料不足")}</span>',
                    unsafe_allow_html=True)
        if df_tf is not None and pd.notna(df_tf["Close"].iloc[-1]):
            st.caption(f"收盤 ${df_tf['Close'].iloc[-1]:,.0f}")

st.markdown("---")

# ============================================================
# 7. 驅動主掃描流程
# ============================================================
all_symbols, name_map, is_fallback = get_futures_symbol_meta()
ticker_snapshot = get_futures_ticker_snapshot()

if is_fallback or not all_symbols:
    st.sidebar.error("🔴 無法從 MEXC 合約 API 取得交易對清單，請檢查網路連線或稍後再試。")
    universe = []
else:
    universe = build_universe(
        all_symbols, name_map, ticker_snapshot, top_n, min_quote_volume_wan * 10_000,
        exclude_stablecoins, exclude_non_crypto,
    )
    st.sidebar.success(f"🟢 母體共 {len(all_symbols)} 個USDT本位永續合約，依條件篩選後鎖定 {len(universe)} 個進行掃描")

st.sidebar.markdown("---")

col_scan, col_info = st.columns([1, 3])
with col_scan:
    run_scan = st.button("🚀 開始全自動高速掃描", use_container_width=True)
with col_info:
    if not universe:
        st.caption("⚠️ 目前掃描母體為空，請確認網路連線或調整「掃描母體設定」中的篩選條件")
    elif not active_directions:
        st.caption("⚠️ 請至少勾選一個訊號方向（做多／做空）")
    else:
        dir_label = "、".join("🟢做多" if d == "long" else "🔴做空" for d in active_directions)
        tf_label = "、".join(TIMEFRAME_CONFIG[tf]["label"] for tf in ACTIVE_TIMEFRAMES) if ACTIVE_TIMEFRAMES else "（尚未勾選任何週期）"
        st.caption(f"本次將掃描方向：{dir_label}　｜　K線級別：{tf_label}")

if run_scan:
    if not universe:
        st.error("目前掃描母體為空，請確認網路連線後再試一次。")
    elif not active_directions:
        st.error("請至少勾選一個訊號方向後再開始掃描。")
    elif not ACTIVE_TIMEFRAMES:
        st.error("請至少勾選一個K線級別後再開始掃描。")
    else:
        t_start = time.time()
        status_box = st.empty()
        tf_label = "、".join(TIMEFRAME_CONFIG[tf]["label"] for tf in ACTIVE_TIMEFRAMES)
        status_box.info(f"📥 正在下載 {len(universe)} 個永續合約的 {tf_label} K線資料，"
                         f"首次執行視母體大小可能需要 30 秒至數分鐘...")

        # 四個K線級別的下載改為同時併行送出（各自仍有獨立快取），而非依序逐一等待，
        # 讓共用的速率限制器隨時保持滿載，減少排程上的閒置時間。
        data_by_tf = {}
        fail_counts = {}
        with ThreadPoolExecutor(max_workers=max(1, len(ACTIVE_TIMEFRAMES))) as tf_executor:
            tf_futures = {tf_executor.submit(download_klines_batch, universe, tf, lookback_bars_global): tf
                          for tf in ACTIVE_TIMEFRAMES}
            for fut in tf_futures:
                tf = tf_futures[fut]
                raw_batch = fut.result()
                fail_counts[tf] = raw_batch.pop("__fail_count__", 0)
                data_by_tf[tf] = raw_batch

        status_box.info("⚡ 數據載入完成，正在平行運算維加斯通道指標與訊號...")

        all_scanned_symbols = set()
        for data_dict in data_by_tf.values():
            all_scanned_symbols.update(data_dict.keys())

        all_hits = []
        with ThreadPoolExecutor() as executor:
            futures_map = {}
            for symbol in all_scanned_symbols:
                raw_by_tf = {tf: data_by_tf[tf].get(symbol) for tf in ACTIVE_TIMEFRAMES}
                fut = executor.submit(
                    analyze_symbol, symbol, raw_by_tf, vegas_params, active_directions,
                    apply_market_filter, btc_1d_status, ACTIVE_TIMEFRAMES,
                )
                futures_map[fut] = symbol
            for future in futures_map:
                hits = future.result()
                for h in hits:
                    sym = h["交易對"]
                    h["幣種名稱"] = name_map.get(sym, sym.split("_")[0])
                    snap = ticker_snapshot.get(sym)
                    if snap is not None:
                        h["24h漲跌幅(%)"] = round(snap["rise_fall_pct"], 2)
                        h["資金費率(%)"] = round(snap["funding_rate_pct"], 4)
                        h["24h成交額(百萬U)"] = fmt_millions(snap["amount24"])
                    else:
                        h["24h漲跌幅(%)"] = np.nan
                        h["資金費率(%)"] = np.nan
                        h["24h成交額(百萬U)"] = np.nan
                    all_hits.append(h)

        t_end = time.time()
        total_fail = sum(fail_counts.values())
        total_attempt = len(universe) * len(ACTIVE_TIMEFRAMES)
        status_box.success(f"✅ 掃描完成！共檢視 {len(all_scanned_symbols)} 個永續合約 × {len(ACTIVE_TIMEFRAMES)} 種K線級別，"
                            f"耗時 {round(t_end - t_start, 2)} 秒。")
        if total_fail > 0:
            fail_detail = "、".join(f"{TIMEFRAME_CONFIG[tf]['label']}失敗{cnt}筆" for tf, cnt in fail_counts.items() if cnt > 0)
            st.warning(f"⚠️ 本次掃描中有 {total_fail}/{total_attempt} 筆「合約×K線級別」資料抓取失敗"
                       f"（{fail_detail}），這些組合會顯示「資料不足」，但不代表該合約真的沒有歷史資料，"
                       f"通常是暫時性的網路延遲或交易所限速，可重新整理再試一次；若持續大量失敗，"
                       f"建議調低「依24小時成交額取前N名」以減少同時請求數量。")

        st.session_state["scan_results"] = all_hits

# ============================================================
# 8. 結果呈現
# ============================================================
results = st.session_state.get("scan_results")

if results is not None:
    st.subheader(f"🎯 掃描結果（共 {len(results)} 筆訊號）")

    if not results:
        st.warning("⚠️ 沒有符合目前參數的訊號。可嘗試調低量能放大倍數、放寬回踩觀察根數，"
                    "或調高「掃描母體」的前N名數量。")
    else:
        df_res = pd.DataFrame(results)
        cols_order = ["交易對", "幣種名稱", "方向", "訊號週期", "訊號觸發時間", "當前收盤價",
                      "24h漲跌幅(%)", "資金費率(%)", "當根K棒成交量", "量能放大倍數",
                      "ATR參考停損價", "1D狀態", "4H狀態", "1H狀態", "15M狀態",
                      "24h成交額(百萬U)", "備註說明"]
        df_res = df_res[[c for c in cols_order if c in df_res.columns]]

        n_long = int((df_res["方向"] == "🟢 做多").sum())
        n_short = int((df_res["方向"] == "🔴 做空").sum())
        m1, m2 = st.columns(2)
        m1.metric("🟢 做多訊號數", n_long)
        m2.metric("🔴 做空訊號數", n_short)

        conviction = (
            df_res.groupby(["交易對", "幣種名稱", "方向"])["訊號週期"]
            .agg(lambda s: "、".join(sorted(set(s))))
            .reset_index()
            .rename(columns={"訊號週期": "命中週期"})
        )
        conviction["命中週期數"] = conviction["命中週期"].apply(lambda s: len(s.split("、")))
        conviction = conviction.sort_values("命中週期數", ascending=False)
        multi_hit = conviction[conviction["命中週期數"] >= 2]

        tv_link_col_config = {
            "交易對": st.column_config.LinkColumn(
                "交易對",
                display_text=r"symbol=MEXC%3A(.*)",
                help="點擊開啟 TradingView 上該合約的走勢圖",
            )
        }

        if not multi_hit.empty:
            st.markdown("#### 🏅 多週期共振排行（同方向同時在 2 個以上K線級別出現訊號）")
            multi_hit_display = multi_hit.copy()
            multi_hit_display["交易對"] = multi_hit_display["交易對"].apply(tradingview_url)
            st.dataframe(multi_hit_display, use_container_width=True, hide_index=True, column_config=tv_link_col_config)
            st.caption("同一交易對、同一方向若同時在多個K線級別觸發訊號，代表多週期共振，"
                       "訊號強度相對較高，但仍需自行確認風險與資金管理。點擊「交易對」欄位可直接開啟 TradingView 圖表。")
            st.markdown("")

        st.markdown("#### 📋 完整訊號明細")
        st.caption("💡 「1D/4H/1H/15M狀態」為該交易對當下四個週期的維加斯通道狀態，僅供多週期對照參考，"
                   "不代表這些週期也同時觸發了進場訊號（訊號觸發週期請看「訊號週期」欄位）。"
                   "「資金費率」為正代表做多方需支付費用給做空方，通常反映市場多頭部位較擁擠，反之亦然。"
                   "點擊「交易對」欄位可直接開啟 TradingView 圖表。")
        df_res_sorted = df_res.merge(conviction[["交易對", "方向", "命中週期數"]], on=["交易對", "方向"], how="left")
        df_res_sorted = df_res_sorted.sort_values(["命中週期數", "訊號觸發時間"], ascending=[False, False])
        df_res_sorted = df_res_sorted.drop(columns=["命中週期數"])

        # CSV 保留原始交易對代碼（方便匯入其他工具），另外附加一欄純文字的TradingView連結
        csv_df = df_res_sorted.copy()
        csv_df["TradingView連結"] = csv_df["交易對"].apply(tradingview_url)
        csv = csv_df.to_csv(index=False).encode("utf-8-sig")

        df_res_display = df_res_sorted.copy()
        df_res_display["交易對"] = df_res_display["交易對"].apply(tradingview_url)
        st.dataframe(df_res_display, use_container_width=True, hide_index=True, column_config=tv_link_col_config)

        st.markdown("###### 📱 手機快速開啟（點這裡，若已安裝 TradingView App 會優先跳轉開啟）")
        st.caption("上面表格的連結是在互動表格元件內用JS觸發，手機瀏覽器常常不會交給TradingView App處理，"
                   "只會用手機瀏覽器打開網頁版。以下改用真正的網頁連結按鈕呈現，手機上點擊時系統才會"
                   "正確判斷「已安裝TradingView App就優先用App開啟」；電腦瀏覽器點擊則一樣是另開分頁顯示網頁版圖表。")
        st.markdown(render_tv_link_list(df_res_sorted), unsafe_allow_html=True)

        st.download_button(
            label="💾 下載本次掃描結果為 CSV",
            data=csv,
            file_name=f"mexc_futures_vegas_scan_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )

        st.markdown("#### 💰 本次訊號 24小時成交額排行（流動性觀察）")
        if "24h成交額(百萬U)" in df_res.columns:
            vol_rank = (
                df_res.drop_duplicates(subset="交易對")
                .set_index("交易對")["24h成交額(百萬U)"]
                .sort_values(ascending=False)
                .head(15)
            )
            if not vol_rank.empty:
                st.bar_chart(vol_rank)
                st.caption("此排行僅列出本次「有觸發訊號」的合約（不分方向），依24小時成交額（百萬USDT）排序。")
else:
    st.info("👈 請至左側側邊欄設定掃描母體與維加斯通道參數，再點擊上方「開始全自動高速掃描」。")

# ============================================================
# 9. 策略說明百科 + 風險提醒
# ============================================================
tab_doc, tab_risk = st.tabs(["📖 策略說明百科", "⚠️ 使用須知與風險提醒"])

with tab_doc:
    st.markdown("""
##### 🎰 維加斯通道 (Vegas Tunnel) 是什麼
以兩組指數移動平均線構成「通道」：小通道 EMA144／EMA169、大通道 EMA576／EMA676
（576、676 恰為 144、169 的 4 倍，是外匯／期貨界常見的維加斯通道週期組合）。

##### 📊 四週期「通道狀態」分類（1D／4H／1H／15m 對照欄位）
每個週期各自獨立分類為以下三種狀態之一：
- **🟢 多頭**：價格在小通道上方，且小通道中心 > 大通道中心，且四條EMA(144/169/576/676)
  相較設定根數前都是上揚。
- **🔴 空頭**：價格在小通道下方，且小通道中心 < 大通道中心，且四條EMA都是下彎。
- **⚪ 盤整**：不符合以上任一情況（例如通道糾結、價格在通道內、EMA方向不一致等）。
- **⚫ 資料不足**：該週期歷史資料不足以計算EMA676，無法判斷。

這四個狀態欄位**僅供對照參考**，用來判斷不同時間尺度的趨勢是否一致（例如1H出現做多
訊號時，若4H、1D也同為多頭，通常代表這個做多訊號站在更順的風向上）；四個週期彼此
獨立判斷，不會互相要求一致才顯示訊號。

##### 🎯 進場訊號判斷（做多／做空鏡射邏輯，1D/4H/1H/15m 各自獨立掃描）
做多訊號需同時符合：
1. 大通道與小通道皆為上揚（即該週期通道結構已經是「多頭」，同上方狀態分類）。
2. 前 X 根K棒內，價格曾經碰觸或跌破小通道，但「當根」價格與EMA12同時重新站回
   小通道之上；且這段期間內不曾出現「收盤同時跌破小通道下緣與EMA12」的確認
   跌破（若曾出現，代表趨勢可能已經真正轉弱，訊號會被排除，不視為買點）。

做空訊號為完全鏡射：通道需為空頭結構，前X根內價格曾反彈觸及/突破小通道，當根
價格與EMA12同時重新跌回小通道之下，且期間未曾出現「收盤同時站上小通道上緣與
EMA12」的確認突破。

這個策略天生需要很長的歷史資料（EMA576/676至少需數百根K棒才會收斂），MEXC合約
K線單次最多可取2000根，已足夠涵蓋1D/4H/1H/15m四個週期的收斂需求，不需要像現貨
版本那樣分頁翻頁抓取。

##### 🌍 關於 BTC 大盤濾網
勾選「依BTC/USDT日線通道狀態篩選訊號」後，做多訊號只在BTC/USDT日線也是多頭結構時
顯示，做空訊號只在BTC日線是空頭結構時顯示；BTC日線處於盤整時，兩個方向都不會顯示，
這是偏保守的設計，用意是避免在大盤方向不明時交易個別合約的雜訊訊號。

##### 🌐 關於「掃描母體」
MEXC永續合約數量遠少於現貨交易對，但仍有數百個，其中包含黃金、原油、美股、指數等
非加密貨幣的CFD商品。本工具預設先依24小時成交額排序，只掃描流動性較佳的前N名，
並可排除穩定幣相關合約與非純加密貨幣合約（此為關鍵字比對的盡力而為過濾，無法保證
100%完整）。
    """)

with tab_risk:
    st.markdown("""
- 本工具所有訊號皆為**歷史技術資料的機械式篩選結果**，不代表任何合約的未來表現，也
  **不構成投資建議**，請自行審慎評估風險並對交易結果負完全責任。
- **永續合約為槓桿商品**：與現貨不同，永續合約本身涉及資金費率（多空雙方定期互相
  支付）、強制平倉、保證金追繳等風險，波動放大的同時虧損也可能放大甚至超過本金，
  請務必先了解相關機制、設定合理槓桿倍數與停損後再操作。
- ATR 參考停損價僅為波動度換算出的參考數字，**不是**保證有效的停損保護，實際下單
  仍須考量流動性、滑點、資金費率成本與個人資金控管原則。
- 「1D/4H/1H/15M狀態」與「訊號」為分開計算的兩件事：狀態欄位是該週期當下的通道
  結構分類，訊號是「回踩/反彈後站回/跌回小通道」的進場事件，請勿將狀態欄位誤認為
  該週期也同時出現了進場訊號。
- MEXC合約也提供股票、商品、指數等CFD商品，本工具已盡力過濾但無法保證完全排除，
  使用「掃描母體設定」時請自行留意結果中的交易對是否為你想要的加密貨幣永續合約。
- 大盤濾網以 BTC/USDT 永續合約的日線通道狀態作為市場多空的代理指標，這是業界常見
  做法，但不代表所有合約都會與 BTC 同步連動，個別合約仍可能出現獨立行情。
- 交易所本身存在API限速、短暫維護或資料延遲的可能性，若掃描過程中部分合約資料
  抓取失敗，該合約本次掃描會被視為「資料暫時無法取得」而非「無訊號」，可稍後
  重新整理再試一次。
- 使用本工具前，建議自行搭配資金費率走勢、未平倉量變化、籌碼面（大額轉帳、交易所
  流入流出）等其他面向做交叉驗證，不應僅憑技術面訊號做出交易決策，並注意個人槓桿
  倍數與部位大小的風險控管。
- 不同國家/地區對加密貨幣衍生品交易的監管規範差異很大，請自行確認所在地區的合法性、
  槓桿/合約交易資格與稅務申報義務。
    """)
