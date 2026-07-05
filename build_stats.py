#!/usr/bin/env python3
"""
Будує stats.json — дерево область -> локації (район / громада / місто).
"""

import json
import sqlite3
import datetime as dt
from pathlib import Path
from collections import defaultdict

from config import REGIONS, ALERT_TYPES

DB = Path(__file__).parent / "data" / "alerts.db"
OUT = Path(__file__).parent / "data" / "stats.json"

# ЗМІНА: поріг «залипання». Відкрита тривога, старша за стільки днів,
# вважається технічним сміттям (немає відбою) і відкидається.
STUCK_DAYS = 7

MONTHS_UA = {1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
             5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
             9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"}

LEVEL_UA = {"oblast": "Область", "raion": "Райони",
            "city": "Міста", "hromada": "Громади", "unknown": "Інше"}


def parse_dt(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=None) if d.tzinfo else d
    except (ValueError, AttributeError):
        return None


def merge_overlaps(intervals):
    """Зливає лише інтервали, що справді перекриваються (строге <)."""
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


def summarize(intervals, open_starts, by_type):
    """Кількість подій, години, помісячно, типи — для одного набору тривог."""
    events = merge_overlaps(intervals)
    total_min = sum((e - s).total_seconds() / 60.0 for s, e in events)
    by_month = defaultdict(lambda: {"count": 0, "minutes": 0.0})
    for s, e in events:
        mk = f"{s.year}-{s.month:02d}"
        by_month[mk]["count"] += 1
        by_month[mk]["minutes"] += (e - s).total_seconds() / 60.0
    # ЗМІНА: відкриті тривоги (без завершення) додаємо в місяць ЇХ ПОЧАТКУ.
    # Вони дають +1 до кількості, але 0 годин (тривалість невідома).
    for s in open_starts:
        mk = f"{s.year}-{s.month:02d}"
        by_month[mk]["count"] += 1
    months = {}
    for mk, v in sorted(by_month.items()):
        months[mk] = {"label": MONTHS_UA.get(int(mk.split("-")[1]), mk),
                      "count": v["count"], "hours": round(v["minutes"] / 60.0, 1)}
    return {
        "count": len(events) + len(open_starts),
        "total_hours": round(total_min / 60.0, 1),
        "avg_minutes": round(total_min / len(events), 1) if events else 0,
        "by_month": months,
        "by_type": {ALERT_TYPES.get(k, k): v for k, v in by_type.items()},
    }


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM alerts").fetchall()
    con.close()

    # ЗМІНА: поточний момент — для оцінки віку відкритих тривог.
    NOW = dt.datetime.now()

    oblasts = {r["uid"]: {"uid": r["uid"], "name": r["name"], "oblast": r["oblast"],
                          "locs": {}} for r in REGIONS}

    for r in rows:
        ruid = r["region_uid"]
        if ruid not in oblasts:
            continue
        loc_uid = r["loc_uid"]
        locs = oblasts[ruid]["locs"]
        if loc_uid not in locs:
            locs[loc_uid] = {"title": r["loc_title"], "level": r["loc_type"] or "unknown",
                             "intervals": [], "open": [], "by_type": defaultdict(int)}
        loc = locs[loc_uid]
        s = parse_dt(r["started_at"]); f = parse_dt(r["finished_at"])
        if not s:
            continue
        if f and f > s:
            loc["by_type"][r["alert_type"] or "unknown"] += 1
            loc["intervals"].append((s, f))
        else:
            # ЗМІНА: відкрита тривога. Якщо вона «висить» довше за поріг —
            # це технічне сміття (немає відбою), відкидаємо повністю.
            age_days = (NOW - s).total_seconds() / 86400
            if age_days > STUCK_DAYS:
                continue
            loc["by_type"][r["alert_type"] or "unknown"] += 1
            loc["open"].append(s)

    regions = []
    for ruid, ob in oblasts.items():
        locs_out = []
        raion_intervals, raion_open, raion_types = [], [], defaultdict(int)
        for loc_uid, loc in ob["locs"].items():
            st = summarize(loc["intervals"], loc["open"], loc["by_type"])
            st.update({"title": loc["title"], "level": loc["level"]})
            locs_out.append(st)
            if loc["level"] == "raion":
                raion_intervals += loc["intervals"]
                raion_open += loc["open"]  # тепер конкатенація списків дат
                for k, v in loc["by_type"].items():
                    raion_types[k] += v

        if raion_intervals or raion_open:
            top = summarize(raion_intervals, raion_open, raion_types)
        else:
            alli, allo, allt = [], [], defaultdict(int)
            for loc in ob["locs"].values():
                alli += loc["intervals"]; allo += loc["open"]
                for k, v in loc["by_type"].items():
                    allt[k] += v
            top = summarize(alli, allo, allt)

        # ЗМІНА (Підхід Б): обласний by_type — валова сума ВСІХ оголошень
        # по всіх рівнях області (район+громада+місто), без злиття.
        # Це число більше за count, бо та сама тривога приходить на кількох
        # рівнях; воно показує загальну кількість оголошень кожного типу.
        oblast_types = defaultdict(int)
        for loc in ob["locs"].values():
            for k, v in loc["by_type"].items():
                oblast_types[k] += v
        top_by_type = {ALERT_TYPES.get(k, k): v for k, v in oblast_types.items()}

        by_level = defaultdict(list)
        for lo in locs_out:
            by_level[lo["level"]].append(lo)
        for lvl in by_level:
            by_level[lvl].sort(key=lambda x: x["total_hours"], reverse=True)

        regions.append({
            "uid": ruid, "name": ob["name"], "oblast": ob["oblast"],
            "count": top["count"], "total_hours": top["total_hours"],
            "avg_minutes": top["avg_minutes"],
            "by_month": top["by_month"], "by_type": top_by_type,
            "levels": {LEVEL_UA.get(k, k): by_level[k] for k in by_level},
        })

    regions.sort(key=lambda r: r["total_hours"], reverse=True)
    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "source": "alerts.in.ua API (накопичувально)",
        "note": ("Область у рейтингу = сума по її районах. У деталях — розбивка "
                 "за рівнями (райони, громади, міста). Рівні не сумуються між "
                 "собою, щоб уникнути подвійного рахунку. Тривоги, що "
                 "перекриваються в часі, рахуються як одна подія."),
        "regions": regions,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {OUT} ({len(regions)} областей)")


if __name__ == "__main__":
    main()
