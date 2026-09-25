# ЗАГЛУШКА — владелец ML2-02 (C4, решение D10).
# Заменить: скачивание bundle-<версия>.7z из общей папки команды, распаковку 7z,
# проверку MANIFEST.sha256 и раскладку в каталог для compose.real.yaml.
# Контракт: параметры -Version и -Dest и итоговая раскладка <Dest>\{data,models,
# configs\features.yaml,reports\intrusion_eventtime_v2_build.json} не меняются;
# docker compose -f compose.yaml -f compose.real.yaml config должен проходить.
#
#   powershell -File scripts\fetch_bundle.ps1 -Version bundle-20260926-1 -Dest .\bundle
# Файл сохранён в UTF-8 с BOM: без BOM Windows PowerShell 5.1 читает кириллицу как ANSI.
param(
    [string]$Version = "bundle-YYYYMMDD-N",
    [string]$Dest = ".\bundle"
)

Write-Output @"
fetch_bundle.ps1 — заглушка ML2-02, ничего не скачивает. Шаги вручную:
  1. Скачать $Version.7z из общей папки команды (эксперты — ссылка в поле
     «Доп. материалы» формы сдачи).
  2. Распаковать:  7z x $Version.7z -o$Dest
     Пароль — тот же, что у датасета организаторов; 7z спросит его сам.
     Пароль нигде не записываем (D10).
  3. Проверить:    Get-Content $Dest\MANIFEST.sha256 | ForEach-Object {
                     `$h, `$f = `$_ -split '\s+\*?', 2
                     if ((Get-FileHash (Join-Path $Dest `$f) -Algorithm SHA256).Hash -ne `$h) { "FAIL `$f" } }
  4. Убедиться, что есть $Dest\configs\features.yaml
     и $Dest\reports\intrusion_eventtime_v2_build.json.
  5. Запустить:    `$env:BUNDLE_DIR = "$Dest"
                   docker compose -f compose.yaml -f compose.real.yaml up -d --build
"@
exit 0
