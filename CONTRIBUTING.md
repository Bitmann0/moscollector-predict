# Участие в разработке

## Начало работы

```bash
git clone https://github.com/Bitmann0/moscollector-predict.git
cd moscollector-predict
make install
make test
```

Рабочие CSV передаются отдельно и помещаются в `data/raw/`. Тестам реальные данные не
нужны: они создают минимальную синтетическую выгрузку во временной директории.

## Ветки и Pull Request

Не работайте напрямую в `main`. Название ветки отражает задачу:

```bash
git switch main
git pull --ff-only
git switch -c feature/feedback-journal
```

Перед отправкой изменений:

```bash
make lint
make test
git add <изменённые-файлы>
git commit -m "feat: add dispatcher feedback"
git push -u origin feature/feedback-journal
```

После этого создайте Pull Request в `main`. Один Pull Request должен решать одну задачу.
Не добавляйте исходные выгрузки, архивы, базы, пароли и персональные данные.

Рекомендуемые префиксы коммитов: `feat`, `fix`, `test`, `docs`, `refactor`, `chore`.

## Каркас и контракты

- Кто владеет каким путём — [docs/OWNERSHIP.md](docs/OWNERSHIP.md). Правка в чужом файле
  идёт через PR с ревью владельца.
- Шапок «ЗАГЛУШКА» в коде не осталось: `git grep -l ЗАГЛУШКА -- ':!*.md'` на 28.09 пуст.
  ML-заглушка `ML_MODE=stub` и синтетический справочник остаются для CI и машин без данных
  заказчика.
- Меняете схему (`backend/app/schemas/`, `ml/src/mkl/product_contract.py`), словарь
  (`contracts/vocabularies.json`) или ответ заглушки — в том же PR запустите
  `python scripts/export_contracts.py` и `cd frontend && npm run gen:api` и закоммитьте
  результат. CI сравнивает `contracts/` и `frontend/src/api/schema.d.ts` с кодом и падает
  на диффе.
- После заморозки v1 (Сб 26.09 12:00) в контракты только добавляются необязательные поля и
  эндпоинты. Ломающее изменение — через PM, с аппрувом обеих сторон.
- Синтетические данные называются «Объект-заглушка N» и несут `source="stub"`. Данных
  заказчика, паролей и `.env` в git нет.
