# -*- coding: utf-8 -*-
"""Согласование документа: автор → корректор → руководитель.

Этапы (jobs.wf_state):
  ""         — ожидает разбора (отчет готов);
  "review"   — на разборе у корректора;
  "returned" — возвращен автору на исправление (с комментарием);
  "approved" — согласован корректором (ждет утверждения, если оно включено);
  "signed"   — утвержден руководителем — финальный этап.

Каждый переход записывается в историю (таблица job_events) и в журнал действий; участникам уходят письма.
Срок этапа: корректору — «Срок разбора, дней» от готовности отчета, руководителю — «Срок утверждения, дней»
от согласования.
"""
import datetime as dt
import threading

from . import core, users

STATES = {"": "ожидает разбора", "review": "на разборе", "returned": "возвращен автору",
          "approved": "согласован корректором", "signed": "утвержден"}
FINAL = ("signed",)

# действие → (из каких этапов, в какой, что нужно уметь, нужен ли комментарий, подпись в истории)
ACTIONS = {
    "take":    (("", "returned"), "review", "review", False, "взят в работу"),
    "return":  (("", "review", "approved"), "returned", "review", True, "возвращен автору"),
    "approve": (("", "review"), "approved", "review", False, "согласован"),
    "sign":    (("approved",), "signed", "sign", False, "утвержден"),
    "fixed":   (("returned",), "", "own", False, "автор сообщил об исправлении"),
    "reopen":  (("approved", "signed", "returned"), "review", "review", True, "открыт заново"),
}
LABELS = {"take": "Взять в работу", "return": "Вернуть автору", "approve": "Согласовать", "sign": "Утвердить",
          "fixed": "Исправлено — проверить снова", "reopen": "Открыть заново"}


def init():
    with core.db() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("wf_state", "TEXT DEFAULT ''"), ("wf_by", "TEXT DEFAULT ''"), ("wf_since", "TEXT DEFAULT ''")):
            if col not in cols:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        c.execute("""CREATE TABLE IF NOT EXISTS job_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, job INTEGER, ts TEXT, user TEXT, action TEXT,
            state_from TEXT, state_to TEXT, comment TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job)")


def enabled(st=None):
    return bool((st or core.load_settings()).get("workflow_enabled"))


def history(jid):
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM job_events WHERE job=? ORDER BY id", (jid,))]


def due(job, st=None):
    """Срок текущего этапа (дата) или None."""
    st = st or core.load_settings()
    state = job.get("wf_state") or ""
    if state in ("", "review"):
        base, days = job.get("finished"), int(st.get("workflow_review_days") or 0)
    elif state == "approved" and st.get("workflow_sign_required", True):
        base, days = job.get("wf_since"), int(st.get("workflow_sign_days") or 0)
    else:
        return None
    if not base or not days:
        return None
    try:
        return (dt.datetime.fromisoformat(str(base)[:19]) + dt.timedelta(days=days)).date()
    except ValueError:
        return None


def overdue(job, st=None):
    d = due(job, st)
    return bool(d and d < dt.date.today())


def allowed(user, job, st=None):
    """Действия, доступные пользователю на текущем этапе."""
    st = st or core.load_settings()
    if not enabled(st) or not job or job.get("status") != "done" or not user:
        return []
    state = job.get("wf_state") or ""
    out = []
    for act, (src, dst, need, _, _) in ACTIONS.items():
        if state not in src:
            continue
        if act == "sign" and not st.get("workflow_sign_required", True):
            continue
        if need == "own":
            if job.get("owner") != user.get("login") and not users.can(user, "review"):
                continue
        elif not users.can(user, need):
            continue
        if act == "reopen" and state == "returned":
            continue                                   # возвращенный документ берут в работу кнопкой «Взять в работу»
        if act == "reopen" and state == "signed" and not users.can(user, "sign"):
            continue                                   # утвержденный документ открывает заново руководитель
        if act in ("approve", "sign") and not st.get("workflow_allow_self") and _self_check(user, job, act):
            continue                                   # разделение обязанностей: свой документ не согласовывают сами
        out.append(act)
    return out


def _self_check(user, job, act):
    """True — пользователь не может выполнить действие сам: документ загрузил он, или (для утверждения) он же его
    согласовал."""
    login = (user or {}).get("login", "")
    if job.get("owner") and job.get("owner") == login:
        return True
    if act == "sign":
        last = [h for h in history(job["id"]) if h["action"] == "approve"]
        if last and last[-1]["user"] == login:
            return True
    return False


def blockers(job, st=None):
    """Почему документ нельзя согласовать (настройки «Согласовывать только…»)."""
    from . import analytics
    st = st or core.load_settings()
    out = []
    if st.get("workflow_require_verified", True) and not job.get("verify_ok"):
        out.append("отчет не прошел машинную проверку оформления (25 требований)")
    if st.get("workflow_require_reviewed", True):
        s = analytics.job_stats(job) or {"findings": []}
        n = sum(1 for f in s["findings"] if f["status"] == "new" and not f.get("auto"))
        if n:
            out.append(f"есть замечания без отметки ✓ или ✕: {n}")
    return out


def act(request, jid, action, comment=""):
    """Выполняет переход. → новый этап. ValueError — переход недопустим."""
    st = core.load_settings()
    user = getattr(request.state, "user", None) if request is not None else None
    job = core.get_job(jid)
    if action not in ACTIONS:
        raise ValueError("неизвестное действие")
    if action not in allowed(user, job, st):
        raise ValueError("это действие сейчас недоступно (этап документа или ваша роль)")
    src, dst, _, need_comment, label = ACTIONS[action]
    comment = (comment or "").strip()[:2000]
    if need_comment and not comment and (action != "return" or st.get("workflow_return_comment", True)):
        raise ValueError("напишите комментарий: что нужно исправить или почему документ открыт заново")
    if action == "approve":
        b = blockers(job, st)
        if b:
            raise ValueError("согласовать пока нельзя: " + "; ".join(b))
    if action == "approve" and not st.get("workflow_sign_required", True):
        dst = "signed"                                 # утверждение руководителем выключено — согласование финальное
    now = dt.datetime.now().isoformat(timespec="seconds")
    with core._lock:
        with core.db() as c:
            cur = c.execute("SELECT wf_state FROM jobs WHERE id=?", (jid,)).fetchone()
            if (cur[0] or "") != (job.get("wf_state") or ""):
                raise ValueError("этап документа уже изменил другой сотрудник — обновите страницу")
            c.execute("UPDATE jobs SET wf_state=?, wf_by=?, wf_since=? WHERE id=?",
                      (dst, (user or {}).get("login", ""), now, jid))
            c.execute("INSERT INTO job_events(job, ts, user, action, state_from, state_to, comment) VALUES(?,?,?,?,?,?,?)",
                      (jid, now, (user or {}).get("login", ""), action, job.get("wf_state") or "", dst, comment))
    users.audit(request, "workflow", jid, {"action": label, "to": STATES[dst], "comment": comment[:300]})
    job_now = core.get_job(jid)

    def send():
        try:
            _notify(st, job_now, action, dst, user or {}, comment)
        except Exception:  # noqa — уведомление не должно мешать переходу
            core.log.exception("уведомление о согласовании %s", jid)
    # почта, Telegram, вебхук и Битрикс24 — в фоне: при недоступном сервере (до 20 с на каждый канал) кнопка
    # согласования не ждала бы минуту, занимая поток веб-сервера
    threading.Thread(target=send, daemon=True, name=f"wf-notify-{jid}").start()
    return dst


def _notify(st, job, action, dst, user, comment):
    from . import notify
    url = notify.link(st, f"/job/{job['id']}")
    who = user.get("name") or user.get("login") or "сотрудник"
    owner = notify.owner_email(job.get("owner"))
    text, to = None, []
    if action == "return":
        text = (f"Документ «{job['name']}» возвращен на исправление.\nКомментарий корректора ({who}): {comment or '—'}\n"
                f"Отчет: {url}")
        to = [owner] if owner else []
    elif dst == "approved" and st.get("workflow_sign_required", True):
        text = f"Документ «{job['name']}» согласован корректором ({who}) и ждет утверждения.\nОткрыть: {url}"
        to = managers_emails(job)
    elif dst == "signed":
        text = f"Документ «{job['name']}» утвержден ({who}). Можно передавать в печать или публиковать.\nОтчет: {url}"
        to = [owner] if owner else []
    elif action == "fixed":
        text = f"Автор сообщил, что документ «{job['name']}» исправлен.\nКомментарий: {comment or '—'}\nОткрыть: {url}"
        to = notify._correctors_of(job.get("department")) or notify.recipients(st)
    if not text:
        return
    subj = f"Корректор: {STATES[dst]} — {job['name']}"
    if st.get("email_enabled") and to:
        notify.send_email(st, subj, text, notify._html(text, url, "Открыть"), sorted(set(to)))
    if st.get("telegram_enabled"):
        import html
        notify.send_telegram(st, f"<b>{html.escape(subj)}</b>\n{html.escape(text)}")
    if st.get("webhook_url"):
        notify.send_webhook(st, {"event": "workflow", "job": job["id"], "name": job["name"], "action": action,
                                 "state": dst, "state_label": STATES[dst], "by": user.get("login", ""),
                                 "comment": comment, "url": url})
    if action == "return" and "returned" in (st.get("bitrix_auto") or []) and st.get("bitrix_webhook"):
        from . import control
        try:
            control.bitrix_task(st, job["id"], f"Документ возвращен корректором на исправление. Комментарий: {comment or '—'}")
        except Exception as e:  # noqa
            core.log.warning("Битрикс24 при возврате %s: %s", job["id"], e)


def reset(request, jid, reason):
    """Повторная проверка документа: согласование начинается заново (с записью в истории)."""
    job = core.get_job(jid)
    if not job or not (job.get("wf_state") or ""):
        return
    user = getattr(request.state, "user", None) if request is not None else None
    now = dt.datetime.now().isoformat(timespec="seconds")
    with core.db() as c:
        c.execute("UPDATE jobs SET wf_state='', wf_by=?, wf_since=? WHERE id=?", ((user or {}).get("login", ""), now, jid))
        c.execute("INSERT INTO job_events(job, ts, user, action, state_from, state_to, comment) VALUES(?,?,?,?,?,?,?)",
                  (jid, now, (user or {}).get("login", ""), "reset", job.get("wf_state") or "", "", reason))


def managers_emails(job):
    d = (job.get("department") or "").strip().lower()
    mans = [u for u in users.all_users() if u.get("active") and u.get("role") == "manager"
            and users.EMAIL_RE.match(u.get("email") or "")]
    same = [u["email"] for u in mans if (u.get("department") or "").strip().lower() == d and d]
    return same or [u["email"] for u in mans]


def summary(scope=None, st=None):
    """Счетчики по этапам для «Контроля» (только готовые задания, видимые пользователю)."""
    st = st or core.load_settings()
    cnt = {k: 0 for k in STATES}
    late = 0
    rows = []
    for j in core.list_jobs(100000, owner=scope):
        if j["status"] != "done":
            continue
        s = j.get("wf_state") or ""
        cnt[s] = cnt.get(s, 0) + 1
        if s not in FINAL:
            d = due(j, st)
            od = bool(d and d < dt.date.today())
            late += int(od)
            if s in ("review", "approved", "returned") or od:
                rows.append({"id": j["id"], "name": j["name"], "state": s, "label": STATES[s], "by": j.get("wf_by") or "",
                             "since": (j.get("wf_since") or "")[:16].replace("T", " "), "due": d.strftime("%d.%m.%Y") if d else "",
                             "overdue": od, "department": j.get("department") or "", "author": j.get("author") or j.get("owner") or ""})
    order = {"approved": 0, "review": 1, "returned": 2, "": 3}
    rows.sort(key=lambda r: (not r["overdue"], order.get(r["state"], 9), r["due"] or "9999"))
    return {"counts": cnt, "late": late, "rows": rows[:500]}
