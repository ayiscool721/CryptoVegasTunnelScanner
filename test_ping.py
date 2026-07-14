#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ping.py
===================================================================
純測試用的極輕量腳本：不做任何MEXC掃描，只單純發一則Telegram訊息，
用來快速驗證「GitHub Actions的排程系統到底有沒有在跑這個repo的workflow」。

確認排程恢復正常之後，建議把這支腳本跟對應的
.github/workflows/test_heartbeat.yml 一起刪掉，避免每分鐘無謂地佔用
Actions執行時間。
"""

import os
import sys
import time
import requests


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 環境變數，中止執行。")
        sys.exit(1)

    now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    text = f"🧪 測試訊息：GitHub Actions 排程在 {now_str} 有正常觸發並執行。"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
    if resp.ok:
        print(f"✅ 測試訊息已送出：{text}")
    else:
        print(f"❌ 測試訊息送出失敗：{resp.status_code} {resp.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
