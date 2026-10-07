#!/usr/bin/env python3
"""Генерирует docs/settings.md из app/settings_schema.py, чтобы справочник всегда совпадал с программой."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.settings_schema import GROUPS, S, PROFILES, DEFAULTS  # noqa

TYPES = {"bool": "переключатель", "int": "целое число", "float": "число", "str": "строка", "text": "многострочный текст",
         "select": "выбор из списка", "multi": "несколько значений", "secret": "секрет (скрыт звездочками)"}


def dflt(s):
    v = s["default"]
    if s["type"] == "bool":
        return "включено" if v else "выключено"
    if s["type"] == "select":
        return dict(s["options"]).get(v, v)
    if s["type"] == "multi":
        d = dict(s["options"])
        return ", ".join(d.get(x, x) for x in v) or "ничего"
    if s["type"] == "secret":
        return "не задан"
    if s["type"] == "text":
        return "см. ниже" if v else "пусто"
    return str(v)


out = ["# Справочник настроек", "",
       "Все настройки открываются в разделе **«Настройки»** (верхнее меню). Справочник ниже создан автоматически из "
       "программы и полностью совпадает с тем, что вы видите в интерфейсе.", "",
       "![Раздел «Настройки»](images/ui-settings.png)", "",
       "## Как устроена страница настроек", "",
       "- **Слева** — разделы и поиск. Начните вводить слово (например, «висяч» или «ключ») — останутся только подходящие настройки.",
       "- **У каждой настройки** — название, служебное имя и значение по умолчанию, короткое описание и кнопка **«Подробнее»**: "
       "что делает, рекомендация и пример.",
       "- **Измененная настройка** подсвечивается рамкой; внизу появляется «Есть несохраненные изменения». Не забудьте нажать **«Сохранить настройки»**.",
       "- **Профиль** подставляет согласованный набор значений: выберите профиль, нажмите «Применить профиль», проверьте и сохраните.",
       "- Внизу страницы — **мастер-промпт** (скачать, загрузить свой, вернуть встроенный) и **резервная копия настроек** (выгрузка, загрузка, сброс).", "",
       "![Подробное описание настройки](images/ui-settings-open.png)", "",
       "![Поиск по настройкам](images/ui-settings-search.png)", "",
       "!!! note \"Когда действуют изменения\"",
       "    Настройки применяются к новым заданиям. Уже выполненное задание можно перепроверить кнопкой «Проверить заново». "
       "Профиль, выбранный при загрузке файла, действует только на эту проверку. Настройка «Одновременных заданий» вступает в силу после перезапуска службы.", "",
       "## Профили", "", "| Настройка | Стандартный | Строгий | Мягкий | Только типографика |", "|---|---|---|---|---|"]
keys = sorted({k for p in PROFILES.values() for k in p})
by = {s["key"]: s for s in S}
for k in keys:
    row = [by[k]["title"]]
    for p in ("standard", "strict", "soft", "typo_only"):
        v = PROFILES[p].get(k, DEFAULTS[k])
        if isinstance(v, bool):
            v = "да" if v else "нет"
        elif isinstance(v, list):
            v = ", ".join(v)
        row.append(str(v))
    out.append("| " + " | ".join(row) + " |")
out += ["", "Остальные настройки во всех профилях одинаковые (значения по умолчанию).", ""]
for g, title, desc in GROUPS:
    out += [f"## {title}", "", desc, ""]
    if g == "ai":
        out += ["![Раздел «Искусственный интеллект»](images/ui-settings-ai.png)", ""]
    for s in [x for x in S if x["group"] == g]:
        out += [f"### {s['title']}", "",
                f"*Имя:* `{s['key']}` · *вид:* {TYPES[s['type']]} · *по умолчанию:* {dflt(s)}", "",
                s["description"], ""]
        if s["options"] and s["type"] in ("select", "multi"):
            out += ["Варианты: " + "; ".join(f"«{t}»" for _, t in s["options"]) + ".", ""]
        if s["type"] == "text" and s["default"]:
            out += ["Значение по умолчанию:", "", "```", s["default"], "```", ""]
        out += [f"**Рекомендация.** {s['recommendation']}", "", f"**Пример.** {s['example']}", ""]
open(os.path.join(ROOT, "docs", "settings.md"), "w", encoding="utf-8").write("\n".join(out))
print("docs/settings.md:", len(S), "настроек")
