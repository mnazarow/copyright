#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Демонстрационные данные для разделов «Аналитика» и «Тренды».

Берет уже проверенные задания из папки данных и создает их «исторические» копии за прошедшие месяцы:
с разными датами, номерами версий, отметками корректора и долей исправленных ошибок. Все копии помечены
словом «демо» в названии и пакетом demo-…, поэтому их легко отличить и удалить.

    sudo -u korrektor /opt/korrektor/venv/bin/python /opt/korrektor/scripts/demo_data.py \\
        --data /var/lib/korrektor --count 60 --days 300
    ... --remove          # удалить все демо-задания

Только для показа и обучения: в рабочей системе удалите демо-данные перед реальной работой.
"""
import argparse
import datetime as dt
import json
import os
import random
import shutil
import sqlite3

EXTRA = [  # типичные смысловые ошибки — имитация находок языковой модели и LanguageTool в демо
    ("Пунктуация", "ai", "Насосы, работают", "Насосы работают", "работают", "Лишняя запятая между подлежащим и сказуемым."),
    ("Пунктуация", "lt", "Однако оборудование", "Однако, оборудование", "Однако,", "Вводное слово выделяется запятой."),
    ("Грамматика", "ai", "в течении года", "в течение года", "течение", "Предлог «в течение» (время) пишется с «е» на конце."),
    ("Грамматика", "lt", "согласно графика", "согласно графику", "графику", "«Согласно» требует дательного падежа."),
    ("Стилистика", "ai", "осуществляет регулировку", "регулирует", "регулирует", "Канцелярит: отглагольное существительное."),
    ("Стилистика", "ai", "является насосом", "— насос", "— насос", "Канцелярит «является»."),
    ("Логика", "ai", "мощность 5 кВт", "мощность 5,5 кВт", "5,5 кВт", "Мощность расходится с таблицей на той же странице."),
    ("Логика", "ai", "3 насоса", "2 насоса", "2 насоса", "В тексте три насоса, на схеме и в таблице — два."),
    ("Орфография", "ai", "прецизионый", "прецизионный", "прецизионный", "Пропущена буква."),
]


AUTHORS = [("Смирнова Е. (демо)", "Маркетинг"), ("Кузнецов Д. (демо)", "Технический отдел"),
           ("Попова М. (демо)", "Маркетинг"), ("Соколов А. (демо)", "Отдел продаж"), ("Лебедева О. (демо)", "Технический отдел")]


def doc_type_of(name):
    n = name.lower()
    if "мини" in n or "обзорн" in n and "каталог" in n:
        return "Обзорный каталог"
    if "брошюр" in n:
        return "Брошюра"
    if "паспорт" in n:
        return "Паспорт изделия"
    return "Каталог"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("KORREKTOR_DATA", "/var/lib/korrektor"), help="папка данных")
    ap.add_argument("--count", type=int, default=60, help="сколько демо-заданий создать")
    ap.add_argument("--days", type=int, default=300, help="на сколько дней назад распределить даты")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--remove", action="store_true", help="удалить все демо-задания")
    a = ap.parse_args()
    db = sqlite3.connect(os.path.join(a.data, "korrektor.db"))
    db.row_factory = sqlite3.Row
    cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    for col, ddl in (("started", "TEXT"), ("owner", "TEXT DEFAULT ''"), ("author", "TEXT DEFAULT ''"),
                     ("department", "TEXT DEFAULT ''"), ("doc_type", "TEXT DEFAULT ''")):
        if col not in cols:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")

    if a.remove:
        ids = [r["id"] for r in db.execute("SELECT id FROM jobs WHERE batch LIKE 'demo-%'")]
        for i in ids:
            shutil.rmtree(os.path.join(a.data, "jobs", str(i)), ignore_errors=True)
        db.execute("DELETE FROM jobs WHERE batch LIKE 'demo-%'")
        db.commit()
        print(f"Удалено демо-заданий: {len(ids)}")
        return

    sp = os.path.join(a.data, "settings.json")
    st = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}
    keep = int(st.get("retention_days", 30) or 0)
    if keep and keep < a.days:
        st["retention_days"] = 0
        json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"Настройка «Хранить задания, дней» была {keep} — установлено 0 (не удалять), иначе служба "
              "удалит демо-задания с прошлыми датами. Верните значение после показа.")

    rnd = random.Random(a.seed)
    src = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status='done' AND batch NOT LIKE 'demo-%' ORDER BY id")]
    src = [j for j in src if os.path.exists(os.path.join(a.data, "jobs", str(j["id"]), "state.json"))]
    seen = set()
    src = [j for j in src if not (j["name"] in seen or seen.add(j["name"]))]
    if not src:
        raise SystemExit("Нет готовых заданий-образцов: сначала проверьте хотя бы один документ.")
    now = dt.datetime.now().replace(microsecond=0)
    plan = []
    for n in range(a.count):
        # ближе к сегодняшнему дню — больше документов (рост использования)
        back = int(a.days * (1 - rnd.random() ** 0.7))
        t = now - dt.timedelta(days=back)
        t = t.replace(hour=rnd.choice([9, 10, 10, 11, 11, 12, 14, 15, 15, 16, 17, 18]), minute=rnd.randint(0, 59))
        if t.weekday() >= 5 and rnd.random() < 0.8:
            t -= dt.timedelta(days=t.weekday() - 4)
        plan.append(t)
    plan.sort()
    version = {}
    made = 0
    for k, t in enumerate(plan):
        base = src[k % len(src)]
        version[base["name"]] = version.get(base["name"], 0) + 1
        v = version[base["name"]]
        progress = k / max(1, len(plan) - 1)                 # 0 — давно, 1 — сейчас
        bid = f"demo-{t:%Y%m%d}"
        name = f"{base['name']} v{v} (демо)"
        sdir = os.path.join(a.data, "jobs", str(base["id"]))
        state = json.load(open(os.path.join(sdir, "state.json"), encoding="utf-8"))
        fs = state["findings"]
        # со временем документы чище: часть ошибок «исправлена» в новых версиях
        ai = (k * 7 + v) % len(AUTHORS)
        author, dept = AUTHORS[ai]
        keep = max(0.35, 1.15 - 0.55 * progress - 0.05 * (v - 1) + rnd.uniform(-0.12, 0.12))
        fs = [f for f in fs if rnd.random() < keep * (0.75 + 0.12 * ai)]
        for typ, rule, src_txt, fix, good, note in EXTRA:
            if rnd.random() < 0.55 - 0.25 * progress:
                fs.append({"page": rnd.randint(1, max(1, state["pages"])), "clause": "текст", "type": typ, "src": src_txt,
                           "bad": src_txt, "fix": fix, "good": good, "note": note + " (демо)", "rule": rule,
                           "status": "new"})
        # решения корректора: свежие задания чаще не просмотрены
        reviewed_p = 0.85 - 0.6 * max(0, progress - 0.8) * 5 if progress > 0.8 else 0.85
        for i, f in enumerate(fs, 1):
            f["id"] = i
            f.pop("edited", None)
            if rnd.random() < reviewed_p:
                bad_rule = f.get("rule") in ("spell", "hanging", "candidate", "lt")
                f["status"] = "rejected" if rnd.random() < (0.38 if bad_rule else 0.08) else "accepted"
            else:
                f["status"] = "new"
        state["findings"] = fs
        state["verify"]["count"] = len(fs)
        if rnd.random() < 0.06:
            state["verify"]["ok"] = False
            for c in state["verify"]["checks"]:
                if c["name"].startswith("«Как правильно» не повторяет"):
                    c["ok"] = False
                    c["detail"] = "демо"
            state["verify"]["passed"] = state["verify"]["total"] - 1
        dur = 6 + state["pages"] * rnd.uniform(0.25, 0.6) + (rnd.uniform(60, 240) if any(f["rule"] == "ai" for f in fs) else 0)
        started = t + dt.timedelta(seconds=rnd.randint(1, 40))
        finished = started + dt.timedelta(seconds=int(dur))
        cur = db.execute("INSERT INTO jobs(batch,name,filename,created,started,finished,status,progress,step,pages,findings,"
                         "verify_ok,verify_score,size,settings,author,department,owner,doc_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (bid, name, base["filename"], t.isoformat(), started.isoformat(), finished.isoformat(), "done", 1,
                          "Готово", state["pages"], len(fs), 1 if state["verify"]["ok"] else 0,
                          f"{state['verify']['passed']}/{state['verify']['total']}", base["size"], "{}", author, dept, "demo", doc_type_of(base["name"])))
        jid = cur.lastrowid
        ddir = os.path.join(a.data, "jobs", str(jid))
        shutil.copytree(sdir, ddir, copy_function=_link, ignore=shutil.ignore_patterns("state.json", "stats.json", "timing.json"))
        json.dump(state, open(os.path.join(ddir, "state.json"), "w", encoding="utf-8"), ensure_ascii=False)
        steps = [("Извлечение текста и координат (extract.py)", 0.25), ("Правила типографики, кодов и данных", 0.1),
                 ("Словарная проверка", 0.15), ("Сборка отчета DOCX и машинная проверка (25 требований)", 0.2)]
        if any(f["rule"] == "ai" for f in fs):
            steps.insert(3, ("ИИ: вычитка смысла по мастер-промпту", 0.0))
        rest = dur - sum(p * dur for _, p in steps)
        timing = [{"step": s, "sec": round(p * dur if p else rest, 2)} for s, p in steps]
        json.dump(timing, open(os.path.join(ddir, "timing.json"), "w", encoding="utf-8"), ensure_ascii=False)
        made += 1
    db.commit()
    print(f"Создано демо-заданий: {made} (период {plan[0]:%d.%m.%Y} — {plan[-1]:%d.%m.%Y})")


def _link(s, d):
    try:
        os.link(s, d)
    except OSError:
        shutil.copy2(s, d)


if __name__ == "__main__":
    main()
