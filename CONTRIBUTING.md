# Участие в разработке

## Начало работы

```bash
git clone https://github.com/Bitmann0/moscollector-predict.git
cd moscollector-predict
make install
make install-ml
make test
```

CSV заказчика передаются отдельно и в git не входят: справочники объектов и каналов для
backend кладутся в `data/raw/`, журналы для ML — в `ml/data/raw/` (`README.md`, «Сборка
бандла из датасета»). Для `make test` реальные данные не нужны: тесты пишут синтетическую
выгрузку во временную директорию, а три теста ML, которым нужны данные заказчика,
`make test-ml` исключает.

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

После этого создайте Pull Request в `main`, по одной задаче на Pull Request. Исходные
выгрузки, архивы, базы, пароли, `.env` и персональные данные в git не добавляются.

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
- Объекты синтетического справочника называются «Объект-заглушка N», ответы ML-заглушки
  несут `source="stub"` (`ml/src/mkl/product_stub.py`).
