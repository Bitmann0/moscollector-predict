#!/bin/sh
# ЗАГЛУШКА — владелец ML2-02 (C4, решение D10).
# Заменить: скачивание bundle-<версия>.7z из общей папки команды, распаковку 7z,
# проверку MANIFEST.sha256 и раскладку в каталог для compose.real.yaml.
# Контракт: аргументы [версия] [каталог] и итоговая раскладка <каталог>/{data,models,
# configs/features.yaml,reports/intrusion_eventtime_v2_build.json} не меняются;
# docker compose -f compose.yaml -f compose.real.yaml config должен проходить.
#
#   sh scripts/fetch_bundle.sh bundle-20260926-1 ./bundle
set -e
VERSION="${1:-bundle-YYYYMMDD-N}"
DEST="${2:-./bundle}"

cat <<EOF
fetch_bundle.sh — заглушка ML2-02, ничего не скачивает. Шаги вручную:
  1. Скачать ${VERSION}.7z из общей папки команды (эксперты — ссылка в поле
     «Доп. материалы» формы сдачи).
  2. Распаковать:  7z x ${VERSION}.7z -o${DEST}
     Пароль — тот же, что у датасета организаторов; 7z спросит его сам.
     Пароль нигде не записываем (D10).
  3. Проверить:    (cd ${DEST} && sha256sum -c MANIFEST.sha256)
  4. Убедиться, что есть ${DEST}/configs/features.yaml
     и ${DEST}/reports/intrusion_eventtime_v2_build.json.
  5. Запустить:    BUNDLE_DIR=${DEST} docker compose -f compose.yaml -f compose.real.yaml up -d --build
EOF
