# Администрирование и безопасность

## 1. Служба и журнал

Корректор работает как служба systemd `korrektor`. Она запускается при включении сервера и перезапускается при сбое.

```bash
korrektor status          # состояние
korrektor logs 200        # последние строки журнала
korrektor follow          # журнал в реальном времени
sudo korrektor restart    # перезапуск после изменения korrektor.env
```

Журнал каждого задания хранится в `/var/lib/korrektor/jobs/<номер>/job.log` и показывается на странице задания («Журнал обработки»). Подробный журнал службы включается в `/etc/korrektor/korrektor.env`: `KORREKTOR_LOG=DEBUG`, затем `sudo korrektor restart`.

## 2. Файл korrektor.env

```ini
KORREKTOR_ADMIN_PASSWORD=...   # пароль входа
KORREKTOR_SECRET=...           # ключ подписи сеансов (смена выводит всех пользователей)
KORREKTOR_DATA=/var/lib/korrektor
KORREKTOR_HOST=0.0.0.0         # 127.0.0.1 — доступ только через nginx
KORREKTOR_PORT=8080
KORREKTOR_LOG=INFO
```

После изменения: `sudo korrektor restart`.

## 3. Файлы задания

| Файл | Содержимое |
|---|---|
| `source.pdf` | загруженный документ |
| `dump.json` | результат `extract.py`: строки, координаты, кандидаты |
| `pages/p001.png …` | изображения страниц |
| `state.json` | замечания с отметками, шапка, результат проверки |
| `findings.json` | данные отчета в формате мастер-промпта (п. 7.1) |
| `*_корректура.docx` | отчет |
| `*_пометки.pdf`, `*.xlsx`, `*.csv` | дополнительные форматы |
| `extract.log`, `verify.log`, `job.log` | журналы этапов |

Эти файлы позволяют повторить любой шаг вручную, как в мастер-промпте:

```bash
cd /var/lib/korrektor/jobs/12
sudo -u korrektor /opt/korrektor/venv/bin/python /opt/korrektor/app/pipeline/scripts/verify.py *_корректура.docx findings.json dump.json
```

## 4. Безопасность

| Мера | Как настроить |
|---|---|
| Пароль входа | задается при установке; смена — `sudo korrektor passwd`. Настройка «Требовать вход по паролю» выключает вход (только для изолированной сети). |
| HTTPS | nginx + сертификат (см. [Установка, п. 6](installation.md#6-nginx-и-https)) |
| Доступ только через nginx | `KORREKTOR_HOST=127.0.0.1` |
| Ограничение по сети | брандмауэр: разрешить порт только из подсети офиса, например `sudo ufw allow from 10.0.0.0/24 to any port 80` |
| Срок хранения документов | Настройки → Система → «Хранить задания, дней» (для конфиденциальных документов — 1–7) |
| Ключи API | хранятся в `/var/lib/korrektor/settings.json` с правами 600, в интерфейсе скрыты; выгрузка настроек по умолчанию без ключей |
| Изоляция службы | отдельный пользователь `korrektor`, `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`, запись только в папку данных |
| Передача документов наружу | только при облачном провайдере ИИ или публичном LanguageTool; для полной изоляции — Ollama и локальный LanguageTool |

### Куда уходят данные

| Компонент | Данные покидают сервер? |
|---|---|
| Извлечение, правила, Hunspell, OCR, сборка отчета | нет |
| LanguageTool локальный | нет |
| LanguageTool публичный (api.languagetool.org) | да, строки текста |
| ИИ: Ollama | нет (если Ollama в вашей сети) |
| ИИ: Anthropic, OpenAI-совместимые | да, текст страниц порциями |

## 5. Производительность

| Документ | Только правила | Правила + ИИ (облако) |
|---|---|---|
| 12 страниц | 3–5 с | 30–60 с |
| 54 страницы, таблицы | 15–25 с | 2–5 мин |
| 70 страниц, графики | 20–40 с | 3–6 мин |

Ускорение: настройка «Одновременных заданий» (по числу ядер, затем перезапуск), «Параллельных запросов» к ИИ, меньшее «Разрешение изображений страниц».

## 6. Мастер-промпт

Встроенный мастер-промпт лежит в `/opt/korrektor/app/prompts/MASTER-PROMPT_CORRECTOR_v5.7.md`. Свой вариант загружается в **Настройки → Мастер-промпт** и сохраняется как `custom_prompt.md` (переживает обновления). Скрипты `extract.py`, `build.py`, `verify.py` остаются встроенными: правила оформления и 25 проверок не меняются при загрузке другого промпта. Чтобы обновить скрипты, выпустите новую версию сервиса (папка `app/pipeline/scripts/`).

## 7. Мониторинг

`GET /api/health` отвечает `{"status":"ok","version":"…"}` без авторизации — подходит для Zabbix, Prometheus blackbox, Uptime Kuma.

```bash
curl -fs http://korrektor.local/api/health || echo "Корректор недоступен"
```

## 8. Разработка и выпуск новой версии

```bash
cd korrektor
python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt
KORREKTOR_DATA=./data KORREKTOR_ADMIN_PASSWORD=dev uvicorn app.main:app --reload --port 8080
```

| Задача | Где менять |
|---|---|
| Новая настройка | `app/settings_schema.py` (страница и справочник строятся автоматически; справочник — `python3 scripts/gen_settings_doc.py`) |
| Новое правило | `app/pipeline/autofix.py` и вызов в `app/pipeline/run.py` |
| Оформление | `app/static/css/style.css`, шаблоны `app/templates/` |
| Документация | `docs/*.md`, схемы `docs/diagrams/*.mmd` |
| Версия | файл `VERSION` |

Сборка дистрибутива: `bash scripts/make_bundle.sh` → `dist/korrektor-<версия>.tar.gz` и `dist/korrektor-<версия>-installer.sh`.
