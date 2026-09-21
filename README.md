# Анализатор изометрий трубопроводов

Локальное приложение для разбора PDF с изометриями. Текущий продукт состоит из FastAPI backend и HTML/JS frontend: загрузка PDF, группировка листов по линиям, локальная карта размеров, проверка кандидатов через провайдера, история запусков, промпты, eval и экспорты.

## Возможности

- Загрузка `Изометрии.pdf` из папки проекта или пользовательского PDF.
- Группировка листов по номерам линий вроде `LC_1031` и `CO_0031`.
- Локальная подготовка листа: числа, координаты, вершины, осевой граф, карта размеров.
- Диагностические PDF-артефакты для проверки разметки и привязки размеров.
- Проверка карты размеров через `DeepSeek API` или `Codex CLI`.
- Версионирование промптов и статистика по версиям.
- Eval по эталонным статусам кандидатов.
- Экспорт JSON, Excel и архива данных для сверки.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Запуск

```powershell
uvicorn server:app --host 127.0.0.1 --port 8600
```

После запуска откройте `http://127.0.0.1:8600`.

## Провайдеры

DeepSeek читает ключ из `.env` или переменных окружения:

```powershell
$env:AI_PROVIDER="deepseek"
$env:DEEPSEEK_API_KEY="sk-..."
$env:DEEPSEEK_MODEL="deepseek-flash"
```

Codex CLI использует локальный вход в аккаунт. В UI можно проверить статус, а для входа выполните:

```powershell
codex login --device-auth
```

Каждый запрос Codex CLI выполняется отдельным `codex exec --ephemeral`, без продолжения предыдущего контекста.

## Текущий Flow

1. Backend читает PDF и кеширует группировку листов.
2. UI показывает найденные группы линий.
3. Пользователь выбирает линии и этап анализа.
4. Локальный pipeline строит числа, вершины, координаты, карту размеров и PDF-разметку.
5. На этапе `Карта размеров + проверка провайдером` провайдер получает payload карты и изображение разметки.
6. Ответ провайдера сохраняется вместе с prompt revision, payload, raw response и eval-оценкой.
7. UI показывает результаты, логи, артефакты, историю и выгрузки.

## API

- `GET /api/config`
- `POST /api/pdf/load-default`
- `POST /api/pdf/load`
- `POST /api/pdf/upload`
- `GET /api/prompts`
- `GET /api/prompts/{name}`
- `POST /api/runs`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/export/json`
- `GET /api/runs/{run_id}/export/excel`
- `GET /api/runs/{run_id}/export/review-data`

## Проверка

```powershell
python -m compileall -q server.py src scripts
python -m pytest -q
```

Для smoke-проверки загрузите локальный `Изометрии.pdf`, найдите `LC_1031`, запустите локальную обработку или полный review при подключенном провайдере.
