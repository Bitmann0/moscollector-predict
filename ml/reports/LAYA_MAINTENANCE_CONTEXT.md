# Laya и новые графики ППР/ТО: парная проверка решения

Проверен **полный** закреплённый checkpoint Laya (`convaiinnovations/laya`,
revision `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851`, `laya==0.3.20`).
Файл весов сверяется по размеру и SHA-256 перед инференсом.

## Постановка

Из апрельского `A_link` shortlist взяты 15 **разных** газовых каналов комплекса
`3828` (старые исходы `y` и риск LightGBM в текст не попали). Для каждого
сохранённого состояния сделана пара сценариев с одинаковой телеметрией:

1. Нет записи графика в контексте.
2. Есть отметка ТО для ГАСБ в **ноябре 2026** из нового Excel. При этом явно
   написано: день работ неизвестен, факт выполнения неизвестен, связь объекта
   с каталогом пока предположительная.

Оба сценария разыгрываются с `asof=2026-11-01`, прогнозом на 02.11.
Апрельская телеметрия перенесена в будущую дату **искусственно**. Это тест
реакции модели на контекст, **не** временной ML-holdout и **не** precision
ремонта. В апреле график не мог быть доступен. Второй вариант входа
дополнительно сообщает Laya, что `A_link` уже выдала ручной диагностический
алерт; это тоже одинаково для двух членов каждой пары.

## Результат на 15 парах

| Что получает Laya | Без графика | С отметкой ТО | Изменился маршрут |
| --- | --- | --- | ---: |
| Только история телеметрии | `monitor` 15/15 | `monitor` 14/15, `verify_planned_work` 1/15 | **1/15** |
| История + уже выданный `A_link` алерт | `verify_planned_work` 15/15 | `verify_planned_work` 15/15 | **0/15** |

Во втором опыте модель предлагает проверять план даже тогда, когда план в
сценарии **не дан**. Значит, её ответ нельзя использовать как надёжный
маршрутизатор между «плановая работа» и «неожиданный обрыв».

Отдельный `noul`-ответ о якобы подтверждённой причине остался ниже 0,5 во
всех 30 вариантах второго опыта, но средний балл вырос с 0,345 без плана до
0,447 с планом. Балл некалиброван; сам график не подтверждает причину.
При загрузке checkpoint пакет также предупредил, что часть температур
`choice` вне допустимого диапазона и заменена на граничное значение. Поэтому
уверенность `choice` не интерпретируется как вероятность.

## Решение

**Не включать Laya в сервисную маршрутизацию по графикам.** Прямой
`maintenance_context` из исходного плана уже есть в `A_link`-алерте, сообщает
точность даты и статус связи с каталогом, не меняет риск и не делает вид, что
работа подтверждена. Laya на этих данных не показала добавочной пользы.

Если появятся реальные подтверждённые работы, решения диспетчера и новые
сигналы после 25.09, можно сформировать отдельную временную проверку
классификации причин. До неё результат — только поведенческий тест.

Воспроизведение (`prepare`/`score` — проектная `.venv`, `infer` —
`..\\.venv-laya`):

```text
python scripts/eval_laya_maintenance.py prepare --max-pairs 15
python scripts/eval_laya_maintenance.py infer --model-dir data/tmp/laya_full \
  --threads 8 --batch-size 10
python scripts/eval_laya_maintenance.py score --report reports/laya_maintenance_telemetry_only.json

python scripts/eval_laya_maintenance.py prepare --max-pairs 15 \
  --include-issued-alert --output data/tmp/laya_maintenance_pairs_issued.jsonl
python scripts/eval_laya_maintenance.py infer \
  --cases data/tmp/laya_maintenance_pairs_issued.jsonl \
  --scores data/tmp/laya_maintenance_scores_issued.jsonl \
  --threads 8 --batch-size 10
python scripts/eval_laya_maintenance.py score \
  --cases data/tmp/laya_maintenance_pairs_issued.jsonl \
  --scores data/tmp/laya_maintenance_scores_issued.jsonl \
  --report reports/laya_maintenance_issued.json
```

Машинные отчёты: [без алерта](laya_maintenance_telemetry_only.json),
[с алертом](laya_maintenance_issued.json). В них зафиксированы хэши входа,
выхода, вопросов и revision модели.
