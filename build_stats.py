#!/usr/bin/env python3
"""
Будує stats.json для дашборду з бази.

По кожній області:
  - кількість подій і сумарна тривалість (год);
  - помісячна розбивка;
  - розбивка за типами загроз.

ЗЛИТТЯ ПЕРЕКРИТТІВ:
  Тривоги, що СПРАВДІ перекриваються в часі (одна почалась, поки інша триває),
  рахуються як одна подія. Тривоги, що лише стикаються кінець-у-початок,
  лишаються окремими. Це прибирає подвійний рахунок, але не злипає все в моноліт.
"""

import json
import sqlite3
import datetime as dt
from pathlib import Path
from collections import defaultdict

from config import REGIONS, ALERT_TYPES

DB = Path(__file__).parent / "data" / "alerts.db"
OUT = Path(__file__).parent / "data" / "stats.json"

MONTHS_UA = {1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
             5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
             9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"}


def parse_dt(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=None) if d.tzinfo else d
    except (ValueError, AttributeError):
        return None


def merge_overlaps(intervals):
    """Зливає лише ті інтервали, що справді перекриваються (строге <)."""
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        last = merged[-1]
        if s < last[1]:
            if e > last[1]:
                last[1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM alerts").fetchall()
    con.close()

    agg = {r["uid"]: {
        "uid": r["uid"], "name": r["name"], "oblast": r["oblast"],
        "intervals": [], "open_count": 0,
        "by_type": defaultdict(int),
    } for r in REGIONS}

    for r in rows:
        uid = r["region_uid"]
        if uid not in agg:
            continue
        s = parse_dt(r["started_at"])
        f = parse_dt(r["finished_at"])
        if not s:
            continue
        agg[uid]["by_type"][r["alert_type"] or "unknown"] += 1
        if f and f > s:
            agg[uid]["intervals"].append((s, f))
        else:
            agg[uid]["open_count"] += 1

    regions = []
    for uid, a in agg.items():
        events = merge_overlaps(a["intervals"])
        count = len(events) + a["open_count"]
        total_min = sum((e - s).total_seconds() / 60.0 for s, e in events)

        by_month = defaultdict(lambda: {"count": 0, "minutes": 0.0})
        for s, e in events:
            mk = f"{s.year}-{s.month:02d}"
            by_month[mk]["count"] += 1
            by_month[mk]["minutes"] += (e - s).total_seconds() / 60.0
        months = {}
        for mk, v in sorted(by_month.items()):
            months[mk] = {
                "label": MONTHS_UA.get(int(mk.split("-")[1]), mk),
                "count": v["count"],
                "hours": round(v["minutes"] / 60.0, 1),
            }

        types = {ALERT_TYPES.get(k, k): v for k, v in a["by_type"].items()}
        regions.append({
            "uid": uid, "name": a["name"], "oblast": a["oblast"],
            "count": count,
            "total_hours": round(total_min / 60.0, 1),
            "avg_minutes": round(total_min / len(events), 1) if events else 0,
            "by_month": months,
            "by_type": types,
        })

    regions.sort(key=lambda r: r["total_hours"], reverse=True)
    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "source": "alerts.in.ua API (накопичувально)",
        "note": ("Тривоги області, що перекриваються в часі, рахуються як одна "
                 "подія. Тривалість — лише для тривог із зафіксованим завершенням. "
                 "Дані з останнього місяця, далі накопичуються щодня."),
        "regions": regions,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {OUT} ({len(regions)} областей)")


if __name__ == "__main__":
    main()
