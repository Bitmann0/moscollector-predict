#!/bin/sh
# Бандл C4 для compose.real.yaml (ML2-02, решение D10): скачать, распаковать 7z,
# проверить MANIFEST.sha256 и раскладку, напечатать BUNDLE_DIR для .env.
#
#   sh scripts/fetch_bundle.sh bundle-20260928-1 ./bundle      # ./<версия>.7z или $BUNDLE_URL/<версия>.7z
#   sh scripts/fetch_bundle.sh ~/Downloads/bundle-20260928-1.7z ./bundle
#   sh scripts/fetch_bundle.sh https://host/path/bundle-20260928-1.7z ./bundle
#   sh scripts/fetch_bundle.sh ./bundle                        # только проверить распакованный
#
# Аргументы: [версия] [каталог]. Версия — bundle-YYYYMMDD-N, путь к .7z, URL или уже
# распакованный каталог (его проверяем на месте, второй аргумент не нужен).
# Каталог (по умолчанию ./bundle) не должен существовать или должен быть пуст.
# BUNDLE_URL — адрес папки с архивами, из которой <версия>.7z скачивается напрямую.
# Пароль архива — тот же, что у датасета организаторов (D10): 7z спросит его сам.
# Без терминала пароль берётся из BUNDLE_PASSWORD; тогда он уходит в 7z ключом -p и
# на время распаковки виден в списке процессов. Пароль нигде не записываем.
# Итог: <каталог>/{data,models,Materials,configs/features.yaml,reports/intrusion_eventtime_v2_build.json}.
# Materials — справочники объектов и каналов для api (BE-03, compose.real.yaml).
set -eu

LAYOUT_DIRS="data models Materials"
LAYOUT_FILES="configs/features.yaml reports/intrusion_eventtime_v2_build.json"

die() {
  echo "fetch_bundle: $*" >&2
  exit 1
}

usage() {
  echo "использование: sh scripts/fetch_bundle.sh <версия|файл.7z|URL|каталог> [каталог]" >&2
  exit 2
}

# Раскладка — контракт томов compose.real.yaml: одиночный файл, которого нет,
# Docker подменит пустым каталогом, и скоринг упадёт на чтении features.yaml.
check_layout() {
  [ -f "$1/MANIFEST.sha256" ] || die "в $1 нет MANIFEST.sha256 — это не бандл C4"
  for d in $LAYOUT_DIRS; do
    [ -d "$1/$d" ] || die "в $1 нет каталога $d/"
  done
  for f in $LAYOUT_FILES; do
    [ -f "$1/$f" ] || die "в $1 нет файла $f"
  done
}

check_sums() {
  n=$(grep -c . "$1/MANIFEST.sha256" || true)
  echo "проверка контрольных сумм: файлов $n"
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$1" && sha256sum -c --quiet MANIFEST.sha256) ||
      die "контрольные суммы не сошлись (список выше): архив повреждён или неполон"
  elif command -v shasum >/dev/null 2>&1; then
    (cd "$1" && shasum -a 256 -c MANIFEST.sha256 >/dev/null) ||
      die "контрольные суммы не сошлись: shasum -a 256 -c MANIFEST.sha256 в $1 покажет какие"
  else
    die "нет ни sha256sum, ни shasum"
  fi
}

done_message() {
  # pwd -W — путь Windows в Git Bash: docker compose на Windows ждёт C:/..., не /c/...
  abs=$(cd "$1" && { pwd -W 2>/dev/null || pwd; })
  echo "бандл проверен: $abs"
  echo "впишите в .env строку:"
  echo "BUNDLE_DIR=$abs"
  echo "и запустите: docker compose -f compose.yaml -f compose.real.yaml up -d --build"
}

find_7z() {
  for c in 7z 7za 7zz; do
    if command -v "$c" >/dev/null 2>&1; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

download() {  # $1 — URL, $2 — файл
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$2" "$1"
  else
    die "нет ни curl, ни wget"
  fi
}

SRC="${1:-}"
DEST="${2:-./bundle}"
DEST="${DEST%/}"
[ -n "$SRC" ] || usage

URL=""
ARCHIVE=""
case "$SRC" in
  http://*|https://*)
    URL="$SRC"
    name=$(basename "${SRC%%\?*}")
    ;;
  *)
    if [ -d "$SRC" ]; then
      check_layout "$SRC"
      check_sums "$SRC"
      done_message "$SRC"
      exit 0
    elif [ -f "$SRC" ]; then
      ARCHIVE="$SRC"
    elif echo "$SRC" | grep -Eq '^bundle-[0-9]{8}-[0-9]+$'; then
      name="$SRC.7z"
      if [ -f "./$name" ]; then
        ARCHIVE="./$name"
      elif [ -n "${BUNDLE_URL:-}" ]; then
        URL="${BUNDLE_URL%/}/$name"
      else
        die "нет ./$name, и не задан BUNDLE_URL; передайте путь к архиву или URL"
      fi
    else
      die "$SRC — не версия bundle-YYYYMMDD-N, не файл, не каталог и не URL"
    fi
    ;;
esac

if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
  die "каталог $DEST не пуст; укажите другой или удалите его"
fi

if [ -n "$URL" ]; then
  # Архив кладём рядом с каталогом назначения; повторный запуск его не качает.
  mkdir -p "$(dirname "$DEST")"
  ARCHIVE="$(dirname "$DEST")/$name"
  if [ -f "$ARCHIVE" ]; then
    echo "уже скачан: $ARCHIVE (удалите файл, чтобы скачать заново)"
  else
    # URL не печатаем: ссылка на общую папку может нести токен доступа.
    echo "скачиваю $name в $ARCHIVE"
    download "$URL" "$ARCHIVE.part" || die "скачать $name не удалось"
    mv "$ARCHIVE.part" "$ARCHIVE"
  fi
fi

SZ=$(find_7z) || die "нет 7z: поставьте p7zip-full или 7zip (Linux), 7-Zip (Windows), sevenzip (macOS)"

# Распаковка во временный каталог рядом: неполный бандл не займёт место готового.
PARTIAL="$DEST.partial"
rm -rf "$PARTIAL"
mkdir -p "$PARTIAL"
echo "распаковка $ARCHIVE"
if [ -n "${BUNDLE_PASSWORD:-}" ]; then
  "$SZ" x -y -bd -p"$BUNDLE_PASSWORD" -o"$PARTIAL" "$ARCHIVE" >/dev/null
else
  "$SZ" x -y -bd -o"$PARTIAL" "$ARCHIVE"
fi || die "7z не распаковал $ARCHIVE: неверный пароль или повреждённый архив"

check_layout "$PARTIAL"
check_sums "$PARTIAL"
[ ! -d "$DEST" ] || rmdir "$DEST"
mv "$PARTIAL" "$DEST"
done_message "$DEST"
