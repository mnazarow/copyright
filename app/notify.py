# -*- coding: utf-8 -*-
"""Уведомления: электронная почта (SMTP), Telegram, вебхук; еженедельная сводка."""
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import core
from .pipeline.common import plural

EVENT_TITLES = {"done": "Отчет готов", "failed": "Проверка не выполнена", "goal": "Цель качества нарушена",
                "digest": "Еженедельная сводка", "test": "Проверка уведомлений", "remind": "Напоминание",
                "report": "Отчет для руководства"}
PLACEHOLDERS = ("name", "pages", "findings", "density", "verify", "author", "department", "doc_type", "url")


def fill_template(tpl, values):
    """Подстановка {name} и т. п.; неизвестные фигурные скобки остаются как есть (без str.format — безопасно)."""
    out = tpl
    for k in PLACEHOLDERS:
        out = out.replace("{" + k + "}", "" if values.get(k) is None else str(values.get(k)))
    return out


def link(st, path):
    base = (st.get("service_url") or "").rstrip("/")
    return base + path if base else path


def recipients(st):
    from .users import EMAIL_RE
    return [x.strip() for x in (st.get("notify_to") or "").replace(",", "\n").replace(";", "\n").splitlines()
            if EMAIL_RE.match(x.strip())]


def owner_email(owner_login):
    from . import users
    u = users.get(owner_login) if owner_login else None
    return u["email"] if u and u.get("email") and users.EMAIL_RE.match(u["email"]) else None


# ------------------------------------------------------------------ каналы
def send_email(st, subject, text, html_body=None, to=None, attachments=None):
    to = to if to is not None else recipients(st)
    if not to:
        return "нет получателей"
    msg = EmailMessage()
    msg["Subject"] = subject
    sender = st.get("smtp_from") or st.get("smtp_user") or "korrektor@localhost"
    msg["From"] = sender if "<" in sender else formataddr(("Корректор", sender))
    msg["To"] = ", ".join(to)
    msg["Message-ID"] = make_msgid(domain="korrektor")
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for name, data, maintype, subtype in attachments or []:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    host, port = st.get("smtp_host"), int(st.get("smtp_port") or 587)
    if not host:
        return "не задан SMTP-сервер"
    sec = st.get("smtp_security", "starttls")
    ctxs = ssl.create_default_context()
    if sec == "ssl":
        srv = smtplib.SMTP_SSL(host, port, timeout=20, context=ctxs)
    else:
        srv = smtplib.SMTP(host, port, timeout=20)
        if sec == "starttls":
            srv.starttls(context=ctxs)
    with srv:
        if st.get("smtp_user"):
            srv.login(st["smtp_user"], st.get("smtp_password") or "")
        srv.send_message(msg)
    return "ok"


def send_telegram(st, text):
    import httpx
    tok, chat = st.get("telegram_token"), st.get("telegram_chat")
    if not tok or not chat:
        return "не заданы токен бота или номер чата"
    r = httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20,
                   json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
    if r.status_code != 200:
        return f"Telegram ответил {r.status_code}: {r.text[:200]}"
    return "ok"


def send_webhook(st, payload):
    import httpx
    url = st.get("webhook_url")
    if not url:
        return "не задан адрес"
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "User-Agent": f"Korrektor/{core.VERSION}",
               "X-Korrektor-Event": payload.get("event", "")}
    if st.get("webhook_secret"):
        headers["X-Korrektor-Signature"] = "sha256=" + hmac.new(st["webhook_secret"].encode(), body,
                                                                hashlib.sha256).hexdigest()
    r = httpx.post(url, content=body, headers=headers, timeout=20)
    return "ok" if r.status_code < 300 else f"ответ {r.status_code}"


def dispatch(st, event, subject, text, html_body, payload, owner=None, owner_text=None):
    """Отправка по всем включенным каналам; ошибки пишутся в журнал и не мешают работе.
    owner_text — свой текст письма автору (шаблон из настроек)."""
    res = {}

    def mail():
        r = send_email(st, subject, text, html_body, recipients(st))
        own = owner_email(owner)
        if own and own not in recipients(st):           # автору — отдельным письмом, без списка внутренних адресатов
            if owner_text:
                url = payload.get("url") if isinstance(payload, dict) else None
                r2 = send_email(st, subject, owner_text, _html(owner_text, url, "Открыть отчет"), [own])
            else:
                r2 = send_email(st, subject, text, html_body, [own])
            r = r2 if r == "нет получателей" else (r if r2 == "ok" else f"{r}; автору: {r2}")
        return r
    for name, enabled, fn in (
            ("почта", st.get("email_enabled"), mail),
            ("Telegram", st.get("telegram_enabled"), lambda: send_telegram(st, f"<b>{html.escape(subject)}</b>\n{html.escape(text)}")),
            ("вебхук", bool(st.get("webhook_url")), lambda: send_webhook(st, payload))):
        if not enabled:
            continue
        try:
            res[name] = fn()
        except Exception as e:  # noqa
            res[name] = f"ошибка: {e}"
        if res[name] != "ok":
            core.log.warning("уведомление «%s» (%s): %s", event, name, res[name])
    return res


# ------------------------------------------------------------------ события заданий
def job_event(jid):
    """Вызывается после окончания задания (в отдельном потоке)."""
    try:
        st = core.load_settings()
        events = st.get("notify_events") or []
        if not (st.get("email_enabled") or st.get("telegram_enabled") or st.get("webhook_url")):
            return
        job = core.get_job(jid)
        if not job:
            return
        state = core.load_state(jid) or {}
        pages = int(job.get("pages") or 0)
        density = round(10.0 * (job.get("findings") or 0) / pages, 2) if pages else None
        from .analytics import density_goal
        goal = density_goal(job.get("doc_type"), st) or 0
        over = bool(goal and density is not None and density > goal)
        url = link(st, f"/job/{jid}")
        files = (state.get("verify") or {}).get("files", {})
        payload = {"event": job["status"], "job": jid, "name": job["name"], "file": job["filename"],
                   "status": job["status"], "pages": pages, "findings": job.get("findings"), "density": density,
                   "verify": job.get("verify_score"), "verify_ok": bool(job.get("verify_ok")), "error": job.get("error"),
                   "author": job.get("author"), "department": job.get("department"), "owner": job.get("owner"),
                   "url": url, "goal_density": goal or None, "goal_exceeded": over,
                   "reports": {k: link(st, f"/api/jobs/{jid}/report?format={k}") for k in files}}
        if job["status"] == "done" and "done" in events:
            lines = [f"Документ: {job['name']}", f"Страниц: {pages}, замечаний: {job.get('findings')}"
                     + (f" ({str(density).replace('.', ',')} на 10 страниц)" if density is not None else ""),
                     f"Машинная проверка оформления: {job.get('verify_score')}"
                     + (" — отчет принят" if job.get("verify_ok") else " — отчет НЕ принят, нужна правка замечаний")]
            if over and st.get("goal_alert", True):
                lines.append(f"! Плотность ошибок выше цели ({goal} на 10 страниц)")
            lines.append(f"Открыть: {url}")
            text = "\n".join(lines)
            custom = (st.get("mail_template_done") or "").strip()
            author_text = None
            if custom:
                author_text = fill_template(custom, {"name": job["name"], "pages": pages, "findings": job.get("findings"),
                                                     "density": density if density is not None else "—",
                                                     "verify": job.get("verify_score"), "author": job.get("author") or "",
                                                     "department": job.get("department") or "",
                                                     "doc_type": job.get("doc_type") or "", "url": url})
            dispatch(st, "done", f"Корректор: отчет готов — {job['name']}", text, _html(text, url, "Открыть отчет"),
                     payload, job.get("owner"), owner_text=author_text)
        if job["status"] == "failed" and "failed" in events:
            text = f"Документ: {job['name']}\nОшибка: {job.get('error')}\nПодробности: {url}"
            dispatch(st, "failed", f"Корректор: проверка не выполнена — {job['name']}", text,
                     _html(text, url, "Открыть задание"), payload, job.get("owner"))
        if job["status"] == "done" and over and "goal" in events and st.get("goal_alert", True):
            text = (f"Документ «{job['name']}»: {str(density).replace('.', ',')} замечаний на 10 страниц при цели "
                    f"{str(goal).replace('.', ',')}.\n"
                    f"Автор: {job.get('author') or '—'}, отдел: {job.get('department') or '—'}.\n{url}")
            dispatch(st, "goal", f"Корректор: превышена цель качества — {job['name']}", text,
                     _html(text, url, "Открыть отчет"), dict(payload, event="goal"))
    except Exception:  # noqa
        core.log.exception("уведомление по заданию %s", jid)


def _after_job(jid):
    job_event(jid)
    try:
        from . import control
        control.bitrix_on_job(jid)
    except Exception:  # noqa
        core.log.exception("Битрикс24 по заданию %s", jid)


def job_event_async(jid):
    threading.Thread(target=_after_job, args=(jid,), daemon=True).start()


def _html(text, url=None, button=None):
    body = "".join(f"<p style='margin:4px 0'>{html.escape(x)}</p>" for x in text.splitlines() if not x.startswith("Открыть:"))
    btn = (f"<p style='margin:18px 0'><a href='{html.escape(url)}' style='background:#0f4382;color:#fff;padding:10px 18px;"
           f"border-radius:4px;text-decoration:none'>{html.escape(button)}</a></p>") if url and url.startswith("http") else ""
    return (f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#1d2433'>"
            f"<div style='border-top:6px solid #0f4382;padding-top:10px;font-size:18px;color:#0f4382'>Корректор</div>"
            f"{body}{btn}<p style='color:#888;font-size:12px'>Письмо отправлено автоматически сервисом «Корректор».</p></div>")


# ------------------------------------------------------------------ сводка и проверка
def digest_text(st):
    from . import analytics
    today = dt.date.today()
    cur = analytics.analytics({"period": "custom", "from": (today - dt.timedelta(days=6)).isoformat(), "to": today.isoformat()})
    prev = analytics.analytics({"period": "custom", "from": (today - dt.timedelta(days=13)).isoformat(),
                                "to": (today - dt.timedelta(days=7)).isoformat()})
    k, p = cur["kpi"], prev["kpi"]

    def ch(a, b, d=2):
        if a is None or b in (None, 0):
            return ""
        return f" ({'+' if a >= b else ''}{round(100 * (a - b) / b, 1)} % к прошлой неделе)"
    goal = float(st.get("goal_density") or 0)
    bad = [r for r in cur["density"] if goal and (r["density"] or 0) > goal][:10]
    lines = [f"Документов: {k['docs']} (неделей ранее {p['docs']}), страниц: {k['pages']}",
             f"Замечаний на 10 страниц: {k['density'] if k['density'] is not None else '—'}{ch(k['density'], p['density'])}",
             f"Точность замечаний: {k['precision'] if k['precision'] is not None else '—'} %, отчетов 25/25: "
             f"{k['verify_rate'] if k['verify_rate'] is not None else '—'} %"]
    if cur["top_rules"]:
        lines.append("Частые ошибки: " + ", ".join(f"{r['label']} ({r['n']})" for r in cur["top_rules"][:5]))
    if bad:
        lines.append(f"Выше цели ({goal} на 10 стр.): " + ", ".join(f"{r['name']} — {r['density']}" for r in bad))
    lines.append(f"Подробнее: {link(st, '/trends?gran=day&period=30')}")
    return "\n".join(lines), {"event": "digest", "kpi": k, "prev_kpi": p, "over_goal": bad}


def send_digest(st):
    text, payload = digest_text(st)
    return dispatch(st, "digest", f"Корректор: сводка за неделю ({dt.date.today():%d.%m.%Y})", text,
                    _html(text, link(st, "/trends"), "Открыть тренды"), payload)


def _read_mark(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write_mark(path, value):
    """Отметка «уже отправлено». Ошибка записи (диск переполнен) прежде терялась при закрытии файла — и рассылка
    повторялась каждые 10 минут; теперь она видна в журнале, а повтор сдерживает отметка в памяти."""
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.replace(tmp, path)
    except OSError:
        core.log.exception("отметка %s не записана", os.path.basename(path))


def digest_loop():
    mark = os.path.join(core.DATA, ".digest_sent")
    done, retry_at = None, 0.0                       # в памяти: не повторять отправленное, ошибки — не чаще раза в час
    while True:
        try:
            st = core.load_settings()
            now = dt.datetime.now()
            week = now.strftime("%G-%V")
            last = _read_mark(mark)
            day, hour = int(st.get("digest_weekday") or 0), int(st.get("digest_hour", 9) or 0)
            due = now.weekday() > day or (now.weekday() == day and now.hour >= hour)   # пропущенный день — догоняем
            if st.get("digest_enabled") and due and last != week and done != week and time.time() >= retry_at:
                retry_at = time.time() + 3600
                send_digest(st)
                done = week
                _write_mark(mark, week)
        except Exception:  # noqa
            core.log.exception("еженедельная сводка")
        time.sleep(600)


# ------------------------------------------------------------------ напоминания
REMIND_FILE = os.path.join(core.DATA, ".remind.json")


def pending_reviews(st, now=None, only=None):
    """Готовые отчеты с замечаниями без отметки дольше remind_days (и не старше remind_max_days).
    → [{job, name, owner, author, department, pending, days, url}]"""
    now = now or dt.datetime.now()
    days, maxd = int(st.get("remind_days") or 3), int(st.get("remind_max_days") or 30)
    out = []
    for j in ([core.get_job(only)] if only else core.list_jobs(100000)):
        if not j or j["status"] != "done" or not j.get("finished"):
            continue
        try:
            age = (now - dt.datetime.fromisoformat(j["finished"][:19])).total_seconds() / 86400
        except ValueError:
            continue
        if not only and (age < days or age > maxd):    # ручное напоминание по документу — без порогов
            continue
        state = core.load_state(j["id"]) or {}
        n = sum(1 for f in state.get("findings", []) if f.get("status", "new") == "new")
        if n:
            out.append({"job": j["id"], "name": j["name"], "owner": j.get("owner") or "", "author": j.get("author") or "",
                        "department": j.get("department") or "", "pending": n, "days": int(age),
                        "url": link(st, f"/job/{j['id']}")})
    return out


def _correctors_of(dept):
    from . import users
    d = (dept or "").strip().lower()
    if not d:
        return []
    return [u["email"] for u in users.all_users() if u.get("active") and u.get("role") == "corrector"
            and (u.get("department") or "").strip().lower() == d and users.EMAIL_RE.match(u.get("email") or "")]


def send_reminders(st, force=False, only=None):
    """→ сводка: сколько документов, писем, итог по каналам. only — номер одного задания (кнопка «Напомнить»)."""
    try:
        sent = json.load(open(REMIND_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        sent = {}
    today = dt.date.today()
    every = int(st.get("remind_days") or 3)
    items = [p for p in pending_reviews(st, only=only) if force or not sent.get(str(p["job"]))
             or (today - dt.date.fromisoformat(sent[str(p["job"])])).days >= every]
    if not items:
        return {"docs": 0, "letters": 0, "result": {}}
    # кому что: автору — его документы; корректорам отдела — документы отдела; без корректоров — общим получателям
    boxes = {}
    for p in items:
        for addr in filter(None, [owner_email(p["owner"])] + (_correctors_of(p["department"]) or recipients(st))):
            boxes.setdefault(addr, []).append(p)
    letters, errors = 0, []
    if st.get("email_enabled"):
        for addr, lst in boxes.items():
            text = ("Есть замечания без отметки ✓ или ✕:\n" + "\n".join(
                f"• {p['name']} — {p['pending']} {plural(p['pending'], 'замечание', 'замечания', 'замечаний')}, "
                f"отчет готов {p['days']} дн. назад: {p['url']}" for p in lst)
                + "\nОтметьте замечания или примите правки — отчет будет пересобран.")
            try:
                r = send_email(st, f"Корректор: неразобранные замечания ({len(lst)})", text, _html(text), [addr])
            except Exception as e:  # noqa
                r = f"ошибка: {e}"
            if r == "ok":
                letters += 1
            else:
                errors.append(f"{addr}: {r}")
    res = {"почта": "ok" if not errors else "; ".join(errors[:3])} if st.get("email_enabled") else {}
    summary = ("Неразобранные замечания:\n" + "\n".join(
        f"• {p['name']} — {p['pending']} шт., {p['days']} дн. ({p['author'] or p['owner'] or '—'})" for p in items[:30]))
    if st.get("telegram_enabled"):
        try:
            res["Telegram"] = send_telegram(st, html.escape(summary))
        except Exception as e:  # noqa
            res["Telegram"] = f"ошибка: {e}"
    if st.get("webhook_url"):
        try:
            res["вебхук"] = send_webhook(st, {"event": "remind", "items": items})
        except Exception as e:  # noqa
            res["вебхук"] = f"ошибка: {e}"
    if not (letters or any(v == "ok" for v in res.values())):
        return {"docs": len(items), "letters": 0, "result": res or {"каналы": "ни один не включен"}}   # повторим позже
    for p in items:
        sent[str(p["job"])] = today.isoformat()
    core.write_json_atomic(REMIND_FILE, {k: v for k, v in sent.items() if v >= (today - dt.timedelta(days=400)).isoformat()})
    return {"docs": len(items), "letters": letters, "result": res}


def remind_loop():
    mark = os.path.join(core.DATA, ".remind_day")
    tries = {}                                       # попыток за день: сбой — повтор через 10 минут, но не больше трех
    while True:
        try:
            st = core.load_settings()
            now = dt.datetime.now()
            today = now.date().isoformat()
            if st.get("remind_enabled") and now.hour >= int(st.get("remind_hour", 10) or 0) \
                    and _read_mark(mark) != today and tries.get(today, 0) < 3:
                tries = {today: tries.get(today, 0) + 1}
                send_reminders(st)
                tries[today] = 3                     # отправлено — сегодня больше не повторять
                _write_mark(mark, today)
        except Exception:  # noqa
            core.log.exception("напоминания")
        time.sleep(600)


def send_test(st):
    text = "Это проверочное сообщение сервиса «Корректор». Если вы его видите — уведомления настроены."
    return dispatch(st, "test", "Корректор: проверка уведомлений", text, _html(text),
                    {"event": "test", "version": core.VERSION, "build": core.BUILD})
