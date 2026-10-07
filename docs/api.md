# REST API и интеграция

Через API документы можно отправлять на проверку из других систем: СЭД, 1С, корпоративного портала, скриптов. Интерактивное описание всех методов (OpenAPI) открывается по адресу `/api/docs`.

## 1. Доступ

1. Откройте **Настройки → Система и безопасность → Токен REST API** и нажмите **«Сгенерировать»**. Скопируйте токен.
2. Передавайте его в заголовке каждого запроса:

```
Authorization: Bearer <токен>
```

Без токена API доступен только из браузера после входа.

## 2. Методы

| Метод | Адрес | Назначение |
|---|---|---|
| GET | `/api/health` | проверка работы (без авторизации) |
| POST | `/api/jobs` | загрузить один или несколько PDF / ZIP; поля формы: `file` (повторяемое), `profile`, `mode` |
| GET | `/api/jobs` | список последних заданий |
| GET | `/api/jobs/{id}` | состояние задания, результат проверки, список замечаний |
| GET | `/api/jobs/{id}/report?format=docx` | скачать отчет: `docx`, `pdf_annot`, `xlsx`, `csv`, `json` |
| POST | `/api/jobs/{id}/findings/{fid}` | изменить замечание: `{"status": "accepted" \| "rejected" \| "new", "fix": "…", "good": "…", "note": "…", "type": "…"}` |
| POST | `/api/jobs/{id}/rebuild` | пересобрать отчет; необязательно `{"summary": "…"}` |

Состояния задания (`status`): `queued` — в очереди, `running` — выполняется (`progress` от 0 до 1, `step` — этап), `done` — готово, `failed` — ошибка (`error`).

## 3. Примеры

### curl (Linux, macOS, Windows 10+)

```bash
TOKEN=ваш_токен
URL=http://korrektor.local
# отправить документ
curl -s -H "Authorization: Bearer $TOKEN" -F "file=@Каталог.pdf" -F "profile=strict" $URL/api/jobs
# {"jobs":[17]}
# дождаться и скачать отчет
while [ "$(curl -s -H "Authorization: Bearer $TOKEN" $URL/api/jobs/17 | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')" != "done" ]; do sleep 5; done
curl -s -H "Authorization: Bearer $TOKEN" -o отчет.docx "$URL/api/jobs/17/report?format=docx"
```

### Python

```python
import time, requests

URL, TOKEN = "http://korrektor.local", "ваш_токен"
H = {"Authorization": f"Bearer {TOKEN}"}

with open("Каталог.pdf", "rb") as f:
    job = requests.post(f"{URL}/api/jobs", headers=H, files={"file": f}).json()["jobs"][0]

while (st := requests.get(f"{URL}/api/jobs/{job}", headers=H).json())["status"] not in ("done", "failed"):
    print(st["step"], round(st["progress"] * 100), "%")
    time.sleep(5)

print("Замечаний:", st["findings"], "проверка:", st["verify_score"])
open("отчет.docx", "wb").write(requests.get(f"{URL}/api/jobs/{job}/report", headers=H, params={"format": "docx"}).content)
```

### PowerShell

```powershell
$URL = "http://korrektor.local"; $H = @{ Authorization = "Bearer ваш_токен" }
$r = Invoke-RestMethod -Uri "$URL/api/jobs" -Method Post -Headers $H -Form @{ file = Get-Item ".\Каталог.pdf" }
$id = $r.jobs[0]
do { Start-Sleep 5; $s = Invoke-RestMethod -Uri "$URL/api/jobs/$id" -Headers $H } until ($s.status -in "done","failed")
Invoke-WebRequest -Uri "$URL/api/jobs/$id/report?format=docx" -Headers $H -OutFile ".\отчет.docx"
```

## 4. Пример ответа `/api/jobs/{id}`

```json
{
  "id": 17, "name": "Каталог", "status": "done", "progress": 1.0, "step": "Готово",
  "pages": 54, "findings": 29, "verify_ok": 1, "verify_score": "25/25",
  "verify": {"ok": true, "passed": 25, "total": 25,
             "files": {"docx": "Каталог_корректура.docx", "pdf_annot": "Каталог_корректура_пометки.pdf"}},
  "findings_list": [
    {"id": 1, "page": 2, "clause": "текст", "type": "Орфография", "src": "ГОСТ P ISO 9001",
     "bad": "ГОСТ P", "fix": "ГОСТ Р ISO 9001", "good": "ГОСТ Р", "status": "new", "rule": "latin",
     "note": "Одиночная латинская буква в русском тексте…"}
  ]
}
```

## 5. Сценарии интеграции

- **Папка-«горячая точка»:** скрипт по расписанию отправляет новые PDF из сетевой папки и кладет отчеты рядом.
- **СЭД:** при переходе документа в статус «На согласовании» отправить PDF и приложить отчет к карточке.
- **Сборка каталога:** после экспорта из InDesign проверить PDF; если в отчете есть замечания типа «Логика», остановить публикацию.
