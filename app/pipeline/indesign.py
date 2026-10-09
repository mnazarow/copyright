# -*- coding: utf-8 -*-
"""Скрипт правок для Adobe InDesign (ExtendScript, .jsx).

Каталоги верстают в InDesign, а на проверку присылают PDF. Скрипт переносит результат корректуры обратно в макет:
  * ошибки с готовым исправлением заменяются в тексте (с проверкой окружения, чтобы не задеть похожие места);
  * исправленные места отмечаются условным текстом «Корректор: исправлено» (зеленая подсветка, не печатается);
  * места «Требует проверки автором» и правки, которые не удалось внести, — условием «Корректор: проверить»
    (желтая подсветка) и заметкой InDesign с пояснением;
  * все изменения — одним шагом отмены (Ctrl+Z), итог — в окне и в файле журнала рядом с макетом.

Скрипт написан на ES3 (ExtendScript), все не-ASCII символы записаны как \\uXXXX — кодировка файла не важна.
Запуск: Окно → Служебные программы → Сценарии → правый щелчок по «Пользователь» → «Показать в Finder/Проводнике»,
положить файл туда и дважды щелкнуть по нему в панели «Сценарии» (или File → Scripts → Other Script в новых версиях).
"""
import datetime as dt
import json
import re

from .common import AUTHOR

CTX = 14            # знаков окружения слева и справа для выбора нужного места


def _parts(f):
    """Объединенное замечание «a … b» → отдельные места (как в правках Word)."""
    bads = [b for b in (f.get("bad") or "").split(" … ") if b.strip()]
    if len(bads) <= 1:
        return [(f.get("bad") or "", f.get("good") or "")]
    goods = (f.get("good") or "").split(" … ")
    if len(goods) == len(bads):
        return list(zip(bads, goods))
    return [(b, "") for b in bads]


def items(findings, accepted_only=False):
    """findings — замечания отчета в порядке его таблицы (run.report_findings): № в скрипте = № в отчете."""
    out = []
    for n, f in enumerate(findings, 1):
        if f.get("status") == "rejected" or (accepted_only and f.get("status") != "accepted"):
            continue
        src = (f.get("src") or "").replace("…", " ").strip()
        author = f.get("type") == AUTHOR
        for bad, good in _parts(f):
            if not bad.strip() or bad.strip() in ("—", "-", "–"):
                continue
            i = src.find(bad)
            pre, post = (src[max(0, i - CTX):i], src[i + len(bad):i + len(bad) + CTX]) if i >= 0 else ("", "")
            fix = not author and good not in ("", "—") and good != bad
            note = f"{(f.get('type') or '').rstrip('.')}. "
            if not author and f.get("fix") not in (None, "", "—"):
                note += f"Как правильно: {f['fix'].rstrip(' .')}. "
            note += f.get("note") or ""
            out.append({"n": n, "page": int(f.get("page") or 0), "bad": bad, "good": good if fix else "",
                        "pre": pre, "post": post, "fix": fix, "note": note.strip()[:900],
                        "status": f.get("status") or "new"})
    return out


def _ascii_js(text):
    """Все не-ASCII знаки → \\uXXXX (допустимо в строках и комментариях JS), чтобы ExtendScript прочитал файл
    в любой кодировке."""
    out = []
    for ch in text:
        o = ord(ch)
        if o < 128:
            out.append(ch)
        elif o <= 0xFFFF:
            out.append("\\u%04x" % o)
        else:                                          # вне BMP — суррогатная пара
            o -= 0x10000
            out.append("\\u%04x\\u%04x" % (0xD800 + (o >> 10), 0xDC00 + (o & 0x3FF)))
    return "".join(out)


TEMPLATE = r"""// Корректор — скрипт правок для Adobe InDesign (CS6 и новее)
// Документ: %(doc)s · задание № %(job)s · создан %(created)s
// Откройте макет в InDesign и запустите скрипт из панели «Сценарии». Все изменения отменяются одним Ctrl+Z.
#target indesign

var KORREKTOR = {
  doc: %(doc_js)s, job: %(job)s, created: %(created_js)s, service: %(service_js)s,
  items: %(items)s
};

function kNorm(s) { return String(s).replace(/[\s   ​­]+/g, " "); }

function kEscFind(s) { return String(s).replace(/\^/g, "^^"); }   // «^» — служебный знак поиска InDesign

function kScore(around, pre, bad, post, offsetInAround) {
  // сколько знаков окружения совпало: слева — с конца pre, справа — с начала post
  var left = kNorm(around.substr(0, offsetInAround)), right = kNorm(around.substr(offsetInAround + bad.length));
  var p = kNorm(pre), q = kNorm(post), sc = 0, i;
  for (i = 1; i <= Math.min(p.length, left.length); i++) { if (p.charAt(p.length - i) === left.charAt(left.length - i)) { sc++; } else { break; } }
  for (i = 0; i < Math.min(q.length, right.length); i++) { if (q.charAt(i) === right.charAt(i)) { sc++; } else { break; } }
  return sc;
}

function kCandidates(doc, text) {
  app.findTextPreferences = NothingEnum.nothing;
  app.changeTextPreferences = NothingEnum.nothing;
  var o = app.findChangeTextOptions;
  o.caseSensitive = true; o.wholeWord = false; o.includeFootnotes = true; o.includeMasterPages = false;
  o.includeHiddenLayers = false; o.includeLockedLayersForFind = false; o.includeLockedStoriesForFind = false;
  app.findTextPreferences.findWhat = kEscFind(text);
  var res = doc.findText();
  app.findTextPreferences = NothingEnum.nothing;
  return res;
}

function kPageOf(t) {
  try { var fr = t.parentTextFrames[0]; if (fr && fr.parentPage) { return fr.parentPage.documentOffset + 1; } } catch (e) {}
  return 0;
}

function kPick(doc, it, text) {
  // → нужное место (Text), null — не найдено, "ambiguous" — несколько равноценных мест
  var found = kCandidates(doc, text), best = null, bestScore = -1, ties = 0, i;
  if (!found || found.length === 0) { return null; }
  var need = Math.min(6, kNorm(it.pre).length + kNorm(it.post).length);
  for (i = 0; i < found.length; i++) {
    var t = found[i], st, from, to, around, sc;
    try {
      st = t.parentStory; from = Math.max(0, t.index - 40); to = Math.min(st.characters.length - 1, t.index + text.length + 40);
      around = st.characters.itemByRange(from, to).contents;
      if (typeof around !== "string") { around = around.join(""); }
      sc = kScore(around, it.pre, text, it.post, t.index - from);
    } catch (e) { sc = 0; }
    if (it.page && kPageOf(t) === it.page) { sc += 2; }          // та же физическая страница — небольшой приоритет
    if (sc > bestScore) { best = t; bestScore = sc; ties = 1; } else if (sc === bestScore) { ties++; }
  }
  if (found.length === 1) { return best; }                      // единственное вхождение в макете
  if (ties > 1 || bestScore < need) { return "ambiguous"; }
  return best;
}

function kCondition(doc, name, color) {
  var c = doc.conditions.itemByName(name);
  if (c.isValid) { return c; }
  return doc.conditions.add({name: name, indicatorColor: color, indicatorMethod: ConditionIndicatorMethod.USE_HIGHLIGHT});
}

function kNote(t, text) {
  try { var n = t.insertionPoints[0].notes.add(); n.texts[0].contents = text; return true; } catch (e) { return false; }
}

function kAsk() {
  var d = app.dialogs.add({name: "Корректор: правки в макете", canCancel: true});
  var col = d.dialogColumns.add();
  col.staticTexts.add({staticLabel: "Задание № " + KORREKTOR.job + ": " + KORREKTOR.items.length + " мест"});
  var cFix = col.checkboxControls.add({staticLabel: "Вносить исправления в текст", checkedState: true});
  var cAcc = col.checkboxControls.add({staticLabel: "Только принятые корректором (✓)", checkedState: false});
  var cMark = col.checkboxControls.add({staticLabel: "Отмечать места условным текстом", checkedState: true});
  var cNote = col.checkboxControls.add({staticLabel: "Добавлять заметки с пояснениями", checkedState: true});
  var ok = d.show(), r = null;
  if (ok) { r = {fix: cFix.checkedState, accepted: cAcc.checkedState, mark: cMark.checkedState, note: cNote.checkedState}; }
  d.destroy();
  return r;
}

function kRun(opt) {
  var doc = app.activeDocument, log = [], cnt = {fixed: 0, marked: 0, already: 0, missing: 0, ambiguous: 0};
  var cFixed = opt.mark ? kCondition(doc, "Корректор: исправлено", UIColors.GRASS_GREEN) : null;
  var cCheck = opt.mark ? kCondition(doc, "Корректор: проверить", UIColors.YELLOW) : null;
  for (var i = 0; i < KORREKTOR.items.length; i++) {
    var it = KORREKTOR.items[i], head = "№ " + it.n + " (стр. " + it.page + ") «" + it.bad + "»";
    if (opt.accepted && it.status !== "accepted") { continue; }
    var t = kPick(doc, it, it.bad);
    if (t === "ambiguous") { cnt.ambiguous++; log.push(head + ": несколько похожих мест — не изменено"); continue; }
    if (t === null) {
      var g = it.fix ? kPick(doc, it, it.good) : null;
      if (g && g !== "ambiguous") { cnt.already++; log.push(head + ": уже исправлено"); }
      else { cnt.missing++; log.push(head + ": не найдено в макете"); }
      continue;
    }
    if (it.fix && opt.fix) {
      try {
        var start = t.index, story = t.parentStory;
        t.contents = it.good;
        if (it.good.length > 0) {
          var nt = story.characters.itemByRange(start, start + it.good.length - 1);
          if (cFixed) { nt.applyConditions([cFixed], false); }
          if (opt.note) { kNote(nt, "Корректор, № " + it.n + ": было «" + it.bad + "». " + it.note); }
        }
        cnt.fixed++; log.push(head + " → «" + it.good + "»: исправлено");
      } catch (e) { cnt.missing++; log.push(head + ": ошибка замены — " + e); }
    } else {
      try {
        if (cCheck) { t.applyConditions([cCheck], false); }
        if (opt.note) { kNote(t, "Корректор, № " + it.n + ": " + it.note); }
        cnt.marked++; log.push(head + ": отмечено для проверки");
      } catch (e2) { log.push(head + ": не удалось отметить — " + e2); }
    }
  }
  return {cnt: cnt, log: log};
}

function kWriteLog(res) {
  var name = "Корректор_задание_" + KORREKTOR.job + "_журнал.txt", folder;
  try { folder = app.activeDocument.filePath; } catch (e) { folder = Folder.desktop; }
  var f = new File(folder + "/" + name);
  f.encoding = "UTF-8"; f.lineFeed = "Windows";
  if (f.open("w")) {
    f.writeln("Корректор: правки в макете «" + app.activeDocument.name + "» по заданию № " + KORREKTOR.job + " (" + KORREKTOR.doc + ")");
    if (KORREKTOR.service) { f.writeln("Отчет: " + KORREKTOR.service); }
    for (var i = 0; i < res.log.length; i++) { f.writeln(res.log[i]); }
    f.close();
    return f.fsName;
  }
  return "";
}

function kMain() {
  if (app.documents.length === 0) { alert("Откройте макет в InDesign и запустите скрипт снова."); return; }
  var opt = kAsk();
  if (!opt) { return; }
  var res = null;
  app.doScript(function () { res = kRun(opt); }, ScriptLanguage.JAVASCRIPT, undefined, UndoModes.ENTIRE_SCRIPT, "Корректор: правки");
  var path = kWriteLog(res), c = res.cnt;
  alert("Корректор: готово.\nИсправлено: " + c.fixed + "\nОтмечено для проверки: " + c.marked +
        "\nУже было исправлено: " + c.already + "\nНе найдено: " + c.missing + "\nНеоднозначно (не изменено): " + c.ambiguous +
        (path ? "\n\nЖурнал: " + path : "") + "\n\nОтменить все изменения — Правка → Отменить (Ctrl+Z).");
}

if (typeof KORREKTOR_TEST === "undefined") { kMain(); }
"""


def build(findings, doc_name, job_id, service_url="", accepted_only=False):
    # имя документа попадает и в комментарий: убираем все, что может закончить строку комментария
    doc_name = re.sub(r"[\x00-\x1f\x7f\u0085\u2028\u2029]", " ", doc_name or "").replace("*/", "* /")[:200]
    data = items(findings, accepted_only)
    js = TEMPLATE % {
        "doc": doc_name, "job": int(job_id), "created": dt.datetime.now().strftime("%d.%m.%Y %H:%M"),
        "doc_js": json.dumps(doc_name, ensure_ascii=False), "created_js": json.dumps(dt.datetime.now().isoformat(timespec="minutes")),
        "service_js": json.dumps(service_url or "", ensure_ascii=False),
        "items": json.dumps(data, ensure_ascii=False, indent=0).replace("\n", "\n    "),
    }
    return _ascii_js(js), len(data)
