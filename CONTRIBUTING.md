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

Перед новым этапом получите актуальные remote-ветки и прочитайте изменения других
участников и открытые PR. Не дублируйте уже сделанную работу. Текущий приоритет команды:
качество данных, смысл целевых меток, временная проверка моделей и воспроизводимость.
Интерфейс вторичен; расширять его следует после проверки основной прогнозной задачи.

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
