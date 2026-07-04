#!/usr/bin/env python3
"""
Збір тривог по областях (alerts.in.ua API), накопичувально.

ЯК ПРАЦЮЄ:
  API віддає історію за останній місяць (period=month_ago). Скрипт запускають
  щодня; він ДОКЛАДАЄ нові тривоги в базу, не дублюючи вже збережені.

  Дедуплікація ЗА ЗМІСТОМ: одна область не може мати дві різні тривоги з тим
  самим часом початку. Це відсікає дублі, які API віддає з різними id при
  щоденному перекритті вікон (інакше активні області роздуваються).

ЛІМІТИ:
  History-ендпоінт: 2 запити/хв. Пауза 31с між областями.

API-ключ — у змінній середовища ALERTS_TOKEN. НЕ вшивайте в код.
"""

import os
import sys
import time
import json
import sqlite3
import datetime as dt
from pathlib import Path
from urllib import request, error

from config import REGIONS

API_BASE = "https://api.alerts.in.ua/v1"
TOKEN = os.environ.get("ALERTS_TOKEN", "").strip()
DB = Path(__file__).parent / "data" / "alerts.db"
HISTORY_DELAY_SEC = 31


def init_db(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            region_uid   INTEGER NOT NULL,
            region_name  TEXT NOT NULL,
            oblast       TEXT,
            alert_type   TEXT,
            started_at   TEXT NOT NULL,
            finished_at  TEXT,
            calculated   INTEGER,
            PRIMARY KEY (region_uid, started_at)
        )
    """)
    con.commit()


def fetch_history(uid):
    url = f"{API_BASE}/regions/{uid}/alerts/month_ago.json"
    req = request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("alerts", []) if isinstance(data, dict) else []
    except error.HTTPError as e:
        if e.code == 429:
            print("    429 (ліміт), чекаю 60с...", file=sys.stderr)
            time.sleep(60)
            return fetch_history(uid)
        print(f"    HTTP {e.code} для uid={uid}: {e.reason}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"    помилка uid={uid}: {e}", file=sys.stderr)
        return []


def upsert(con, region, alerts):
    added = 0
    for a in alerts:
        if not isinstance(a, dict):
            continue
        started = a.get("started_at")
        if not started:
            continue
        cur = con.execute(
            """INSERT OR IGNORE INTO alerts
               (region_uid, region_name, oblast, alert_type,
                started_at, finished_at, calculated)
               VALUES (?,?,?,?,?,?,?)""",
            (
                region["uid"],
                a.get("location_title") or region["name"],
                a.get("location_oblast") or region.get("oblast"),
                a.get("alert_type"), started,
                a.get("finished_at"),
                1 if a.get("calculated") else 0,
            ),
        )
        added += cur.rowcount
    con.commit()
    return added


def main():
    if not TOKEN:
        print("ПОМИЛКА: не задано ALERTS_TOKEN.\n"
              "Отримайте токен на https://alerts.in.ua/api-request і задайте:\n"
              "  export ALERTS_TOKEN='ваш_ключ'", file=sys.stderr)
        sys.exit(1)

    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    init_db(con)

    print(f"Збираю місячну історію по {len(REGIONS)} областях "
          f"(~{len(REGIONS)*HISTORY_DELAY_SEC//60} хв через ліміт API)...")
    total = 0
    for i, region in enumerate(REGIONS, 1):
        print(f"[{i}/{len(REGIONS)}] {region['name']} (uid={region['uid']})")
        alerts = fetch_history(region["uid"])
        added = upsert(con, region, alerts)
        total += added
        print(f"    отримано {len(alerts)}, нових у базі: {added}")
        if i < len(REGIONS):
            time.sleep(HISTORY_DELAY_SEC)

    con.close()
    print(f"\nГотово. Нових записів: {total}. База: {DB}")


if __name__ == "__main__":
    main()
