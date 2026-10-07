# -*- coding: utf-8 -*-
"""Пакет документов: сравнение версий (п. 3.8) и сверка реквизитов между документами (п. 5.15)."""
import difflib
import json
import os
import re

from . import core
from .pipeline.autofix import nearest_address
from .pipeline.common import AUTHOR, norm, yo

PHONE = re.compile(r"\+7[\s(]*\d{3}[\s)]*\d{3}[\s-]?\d{2}[\s-]?\d{2}|\+998[\d\s]{9,13}")
MAIL = re.compile(r"[\w.]+@[\w.-]+\.\w+")
ADDR = re.compile(r"(ул\.|пр-т|пр-кт|проспект|пер\.|ш\.|наб\.|мкр|вл\.|д\.\s*\d)")


def load_dump(jid):
    p = os.path.join(core.job_dir(jid), "dump.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def analyze(jobs, st):
    docs = {j["id"]: (j, load_dump(j["id"])) for j in jobs if j["status"] == "done"}
    docs = {k: v for k, v in docs.items() if v[1]}
    res = {"groups": [], "requisites": []}
    # версии
    if st.get("batch_compare", True):
        thr = int(st.get("batch_similarity") or 50) / 100
        texts = {k: [" ".join(ln["text"] for ln in p["lines"]) for p in d["lines"]] for k, (j, d) in docs.items()}
        used = set()
        for a in docs:
            if a in used:
                continue
            g = [a]
            for b in docs:
                if b != a and b not in used and len(texts[a]) == len(texts[b]) and \
                        sum(x == y for x, y in zip(texts[a], texts[b])) >= thr * len(texts[a]):
                    g.append(b)
            if len(g) > 1:
                used |= set(g)
                base = docs[a][1]["lines"]
                diffs = []
                for b in g[1:]:
                    pages = []
                    for pa, pb in zip(base, docs[b][1]["lines"]):
                        la = [ln["text"] for ln in pa["lines"]] + pa["head"] + pa["foot"]
                        lb = [ln["text"] for ln in pb["lines"]] + pb["head"] + pb["foot"]
                        if la != lb:
                            d = [x for x in difflib.unified_diff(la, lb, lineterm="", n=0)
                                 if not x.startswith(("@@", "---", "+++"))]
                            pages.append({"page": pa["pdf_page"], "diff": d[:40]})
                    diffs.append({"job": b, "name": docs[b][0]["name"], "pages": pages})
                res["groups"].append({"base": a, "base_name": docs[a][0]["name"], "others": diffs})
    # реквизиты
    if st.get("batch_requisites", True):
        seen = {}
        for k, (j, d) in docs.items():
            for p in d["lines"]:
                lines = [ln["text"].strip() for ln in p["lines"]]
                for i, t in enumerate(lines):
                    for m in list(PHONE.finditer(t)) + list(MAIL.finditer(t)):
                        key = re.sub(r"[\s()-]", "", m.group(0)).lower()
                        addr = nearest_address(lines, i) or ""
                        if addr:
                            seen.setdefault(key, {}).setdefault(k, {})[addr] = p["pdf_page"]
        for key, per in seen.items():
            if len(per) < 2:
                continue
            for k, addrs in per.items():
                others = {norm(a).lower(): (kk, a) for kk, aa in per.items() if kk != k for a in aa}
                for a, pg in addrs.items():
                    if norm(a).lower() not in others:
                        alt = sorted({o[1] for o in others.values()},
                                     key=lambda x: -difflib.SequenceMatcher(None, x.lower(), a.lower()).ratio())
                        where = sorted({docs[o[0]][0]["name"] for o in others.values()})
                        res["requisites"].append({"job": k, "name": docs[k][0]["name"], "page": pg, "key": key,
                                                  "address": a, "others": alt[:3], "docs": where[:6]})
    return res


def maybe_finish(batch):
    jobs = core.list_jobs(batch=batch)
    if len(jobs) < 2 or any(j["status"] in ("queued", "running", "uploading") for j in jobs):
        return
    st = core.load_settings()
    res = analyze(jobs, st)
    core.write_json_atomic(os.path.join(core.BATCHES, batch + ".json"), res, indent=1)
    # расхождения реквизитов → в отчет документа, где адрес отличается
    from .pipeline import run
    by_job = {}
    seen_addr = set()
    for r in res["requisites"]:
        if (r["job"], r["address"]) in seen_addr:
            continue
        seen_addr.add((r["job"], r["address"]))
        by_job.setdefault(r["job"], []).append(r)
    for jid, items in by_job.items():
        with core.state_lock(jid):
            state = core.load_state(jid)
            if not state:
                continue
            have = {norm(f["src"]) for f in state["findings"]}
            for r in items:
                src = yo(r["address"])
                if norm(src) in have:
                    continue
                state["findings"].append({
                    "id": max([f["id"] for f in state["findings"]] + [0]) + 1, "status": "new", "rule": "batch",
                    "page": r["page"], "clause": "контакты", "type": AUTHOR, "src": src, "bad": src, "fix": "—", "good": "—",
                    "note": f"Сверка с другими документами пакета: рядом с «{r['key']}» в документах {', '.join(r['docs'][:3])} "
                            f"указан адрес «{yo(r['others'][0]) if r['others'] else ''}» (п. 5.15)."})
                have.add(norm(src))
            job = core.get_job(jid)
            if not job:
                continue
            s2 = dict(st)
            s2.update(json.loads(job.get("settings") or "{}"))
            state["verify"] = run.build_report(core.job_dir(jid), state, s2, lambda m: None)
            core.save_state(jid, state)
            v = state["verify"]
            core.update_job(jid, findings=v.get("count", 0), verify_ok=1 if v.get("ok") else 0,
                            verify_score=f"{v.get('passed', 0)}/{v.get('total', 0)}")


def load(batch):
    p = os.path.join(core.BATCHES, batch + ".json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
