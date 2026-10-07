# -*- coding: utf-8 -*-
"""«Контроль разбора»: какие отчеты ждут решения корректора, сколько дней и кто отвечает; задачи в Битрикс24.

Данные берутся из сводок заданий (stats.json), поэтому страница открывается быстро и на тысячах документов.
"""
import datetime as dt
import json
import os
import re

from . import analytics, core, notify, users


def _age_days(iso, now):
    try:
        return max(0, (now - dt.datetime.fromisoformat(str(iso)[:19])).total_seconds() / 86400)
    except (TypeError, ValueError):
        return None


def correctors_by_dept():
    out = {}
    for u in users.all_users():
        if u.get("active") and u.get("role") == "corrector":
            out.setdefault((u.get("department") or "").strip().lower(), []).append(u.get("name") or u["login"])
    return out


def overview(scope=None, department="", st=None):
    """→ {rows: [...], depts: [...], kpi: {...}} — документы с замечаниями без отметки."""
    st = st or core.load_settings()
    now = dt.datetime.now()
    warn = int(st.get("remind_days") or 3)
    try:
        reminded = json.load(open(notify.REMIND_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        reminded = {}
    corr = correctors_by_dept()
    rows = []
    for j in core.list_jobs(100000, owner=scope):
        if j["status"] != "done":
            continue
        if department and (j.get("department") or "") != department:
            continue
        s = analytics.job_stats(j)
        if not s:
            continue
        fs = s["findings"]
        new = sum(1 for f in fs if f["status"] == "new" and not f.get("auto"))
        if not new:
            continue
        age = _age_days(j.get("finished") or j.get("created"), now)
        dept = j.get("department") or ""
        rows.append({"id": j["id"], "name": j["name"], "author": j.get("author") or "", "owner": j.get("owner") or "",
                     "department": dept, "doc_type": j.get("doc_type") or "", "new": new, "total": len(fs),
                     "done_share": round(100 * (len(fs) - new) / len(fs)) if fs else 0,
                     "age": round(age, 1) if age is not None else None, "overdue": age is not None and age >= warn,
                     "correctors": corr.get(dept.strip().lower(), []), "reminded": reminded.get(str(j["id"]), ""),
                     "verify_ok": bool(j.get("verify_ok"))})
    rows.sort(key=lambda r: (-(r["age"] or 0), -r["new"]))
    depts = {}
    for r in rows:
        d = depts.setdefault(r["department"] or "не указан", {"label": r["department"] or "не указан", "docs": 0,
                                                             "findings": 0, "overdue": 0, "oldest": 0})
        d["docs"] += 1
        d["findings"] += r["new"]
        d["overdue"] += int(r["overdue"])
        d["oldest"] = max(d["oldest"], r["age"] or 0)
    kpi = {"docs": len(rows), "findings": sum(r["new"] for r in rows), "overdue": sum(1 for r in rows if r["overdue"]),
           "oldest": max((r["age"] or 0 for r in rows), default=0), "warn_days": warn}
    return {"rows": rows, "depts": sorted(depts.values(), key=lambda d: -d["findings"]), "kpi": kpi}


# ------------------------------------------------------------------ Битрикс24
def _b24(st, method, payload):
    import httpx
    base = (st.get("bitrix_webhook") or "").strip().rstrip("/")
    if not re.match(r"^https://[^/]+/rest/\d+/[^/]+$", base):
        raise ValueError("адрес вебхука Битрикс24 — вида https://компания.bitrix24.ru/rest/1/ключ/")
    r = httpx.post(f"{base}/{method}.json", json=payload, timeout=20)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in data:
        raise ValueError(f"Битрикс24: {data.get('error_description') or data.get('error') or r.status_code}")
    return data.get("result")


def bitrix_user_id(st, email):
    if not email:
        return None
    res = _b24(st, "user.get", {"FILTER": {"EMAIL": email, "ACTIVE": True}})
    return int(res[0]["ID"]) if res else None


def bitrix_task(st, jid, reason):
    """Задача автору документа в Битрикс24 со ссылкой на отчет. → номер задачи."""
    job = core.get_job(jid)
    if not job:
        raise ValueError("задание не найдено")
    s = analytics.job_stats(job) or {"findings": []}
    new = sum(1 for f in s["findings"] if f["status"] == "new")
    url = notify.link(st, f"/job/{jid}")
    who = users.get(job.get("owner") or "") or {}
    rid = bitrix_user_id(st, who.get("email")) or int(st.get("bitrix_default_responsible") or 0) or None
    if not rid:
        raise ValueError("не найден ответственный: у автора нет почты, совпадающей с Битрикс24, и не задан "
                         "ответственный по умолчанию")
    days = int(st.get("bitrix_deadline_days") or 3)
    fields = {"TITLE": f"Корректура: {job['name']}"[:250],
              "DESCRIPTION": (f"{reason}\n\nДокумент: {job['name']}\nСтраниц: {job.get('pages')}, замечаний: "
                              f"{job.get('findings')} (без решения: {new}), проверка оформления: {job.get('verify_score')}\n"
                              f"Отчет: {url}\n\nЗадача создана сервисом «Корректор»."),
              "RESPONSIBLE_ID": rid,
              "DEADLINE": (dt.datetime.now() + dt.timedelta(days=days)).replace(microsecond=0).isoformat()}
    if st.get("bitrix_group_id"):
        fields["GROUP_ID"] = int(st["bitrix_group_id"])
    res = _b24(st, "tasks.task.add", {"fields": fields})
    task_id = (res or {}).get("task", {}).get("id") if isinstance(res, dict) else None
    mark = os.path.join(core.job_dir(jid), "bitrix.json")
    core.write_json_atomic(mark, {"task": task_id, "time": dt.datetime.now().isoformat(timespec="seconds"),
                                  "reason": reason})
    return task_id


def bitrix_task_of(jid):
    try:
        return json.load(open(os.path.join(core.job_dir(jid), "bitrix.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bitrix_on_job(jid):
    """После проверки: задача автору, если отчет не принят машинной проверкой или нарушена цель качества."""
    st = core.load_settings()
    if not st.get("bitrix_webhook") or not st.get("bitrix_auto") or bitrix_task_of(jid):
        return
    job = core.get_job(jid)
    if not job or job["status"] != "done":
        return
    pages = int(job.get("pages") or 0)
    density = 10.0 * (job.get("findings") or 0) / pages if pages else 0
    goal = analytics.density_goal(job.get("doc_type"), st) or 0
    reasons = []
    if "goal" in (st.get("bitrix_auto") or []) and goal and density > goal:
        reasons.append(f"Плотность ошибок {density:.2f} на 10 страниц выше цели {goal}.".replace(".", ",", 1))
    if "unverified" in (st.get("bitrix_auto") or []) and not job.get("verify_ok"):
        reasons.append("Отчет не прошел машинную проверку оформления — нужна правка замечаний.")
    if "always" in (st.get("bitrix_auto") or []) and job.get("findings"):
        reasons.append("Отчет готов: примите или отклоните замечания и исправьте документ.")
    if reasons:
        try:
            bitrix_task(st, jid, " ".join(reasons))
        except Exception as e:  # noqa
            core.log.warning("Битрикс24, задание %s: %s", jid, e)
