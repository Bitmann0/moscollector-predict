# Бандл C4 для compose.real.yaml (ML2-02, решение D10): скачать, распаковать 7z,
# проверить MANIFEST.sha256 и раскладку, напечатать BUNDLE_DIR для .env.
#
#   powershell -File scripts\fetch_bundle.ps1 -Version bundle-20260928-1 -Dest .\bundle
#   powershell -File scripts\fetch_bundle.ps1 -Version $HOME\Downloads\bundle-20260928-1.7z -Dest .\bundle
#   powershell -File scripts\fetch_bundle.ps1 -Version https://host/path/bundle-20260928-1.7z
#   powershell -File scripts\fetch_bundle.ps1 -Version .\bundle      # только проверить распакованный
#
# -Version — bundle-YYYYMMDD-N (ищем .\<версия>.7z, затем $env:BUNDLE_URL/<версия>.7z),
# путь к .7z, URL или уже распакованный каталог (его проверяем на месте, -Dest не нужен).
# -Dest (по умолчанию .\bundle) не должен существовать или должен быть пуст.
# Пароль архива — тот же, что у датасета организаторов (D10): 7z спросит его сам.
# Без консоли пароль берётся из $env:BUNDLE_PASSWORD; тогда он уходит в 7z ключом -p и
# на время распаковки виден в списке процессов. Пароль нигде не записываем.
# Итог: <Dest>\{data,models,configs\features.yaml,reports\intrusion_eventtime_v2_build.json}.
# Файл сохранён в UTF-8 с BOM: без BOM Windows PowerShell 5.1 читает кириллицу как ANSI.
param(
    [string]$Version = "",
    [string]$Dest = ".\bundle"
)

$ErrorActionPreference = "Stop"
# Индикатор Invoke-WebRequest в PowerShell 5.1 замедляет скачивание в разы.
$ProgressPreference = "SilentlyContinue"

$LayoutDirs = @("data", "models")
$LayoutFiles = @("configs\features.yaml", "reports\intrusion_eventtime_v2_build.json")

function Fail([string]$Message) {
    [Console]::Error.WriteLine("fetch_bundle: $Message")
    exit 1
}

# Раскладка — контракт томов compose.real.yaml: одиночный файл, которого нет,
# Docker подменит пустым каталогом, и скоринг упадёт на чтении features.yaml.
function Test-Layout([string]$Dir) {
    if (-not (Test-Path -LiteralPath (Join-Path $Dir "MANIFEST.sha256") -PathType Leaf)) {
        Fail "в $Dir нет MANIFEST.sha256 — это не бандл C4"
    }
    foreach ($d in $LayoutDirs) {
        if (-not (Test-Path -LiteralPath (Join-Path $Dir $d) -PathType Container)) {
            Fail "в $Dir нет каталога $d\"
        }
    }
    foreach ($f in $LayoutFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $Dir $f) -PathType Leaf)) {
            Fail "в $Dir нет файла $f"
        }
    }
}

# SHA-256 средствами .NET, а не Get-FileHash: Windows PowerShell 5.1, запущенный из
# PowerShell 7, наследует чужой PSModulePath и Get-FileHash не находит.
function Get-Sha256([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead((Convert-Path -LiteralPath $Path))
    try {
        return -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString("x2") })
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
}

# То же, что sha256sum -c: строка «хеш, пробел, пробел или *, путь от корня бандла».
function Test-Manifest([string]$Dir) {
    $lines = @(Get-Content -LiteralPath (Join-Path $Dir "MANIFEST.sha256") -Encoding UTF8 |
        Where-Object { $_.Trim() })
    Write-Output "проверка контрольных сумм: файлов $($lines.Count)"
    $bad = 0
    foreach ($line in $lines) {
        if ($line -notmatch '^([0-9a-fA-F]{64}) [ *](.+)$') {
            Write-Output "строка не в формате sha256sum: $line"
            $bad++
            continue
        }
        $hash = $Matches[1]
        $rel = $Matches[2]
        $path = Join-Path $Dir $rel
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            Write-Output "${rel}: FAILED open or read"
            $bad++
            continue
        }
        # -ne сравнивает строки без учёта регистра.
        if ((Get-Sha256 $path) -ne $hash) {
            Write-Output "${rel}: FAILED"
            $bad++
        }
    }
    if ($bad -gt 0) {
        Fail "контрольные суммы не сошлись у $bad файлов (список выше): архив повреждён или неполон"
    }
}

function Write-Done([string]$Dir) {
    # Прямые слеши: docker compose читает .env без экранирования, C:/... надёжнее C:\...
    $abs = (Resolve-Path -LiteralPath $Dir).ProviderPath -replace '\\', '/'
    Write-Output "бандл проверен: $abs"
    Write-Output "впишите в .env строку:"
    Write-Output "BUNDLE_DIR=$abs"
    Write-Output "и запустите: docker compose -f compose.yaml -f compose.real.yaml up -d --build"
}

function Find-SevenZip {
    foreach ($name in @("7z", "7za", "7zz")) {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($cmd) { return $cmd.Source }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base -and (Test-Path -LiteralPath (Join-Path $base "7-Zip\7z.exe"))) {
            return (Join-Path $base "7-Zip\7z.exe")
        }
    }
    return $null
}

if (-not $Version) {
    [Console]::Error.WriteLine(
        "использование: scripts\fetch_bundle.ps1 -Version <версия|файл.7z|URL|каталог> [-Dest каталог]")
    exit 2
}
$Dest = $Dest.TrimEnd('\', '/')

$url = $null
$archive = $null
if ($Version -match '^https?://') {
    $url = $Version
    $name = Split-Path -Leaf (($Version -split '\?', 2)[0])
} elseif (Test-Path -LiteralPath $Version -PathType Container) {
    Test-Layout $Version
    Test-Manifest $Version
    Write-Done $Version
    exit 0
} elseif (Test-Path -LiteralPath $Version -PathType Leaf) {
    $archive = $Version
} elseif ($Version -match '^bundle-\d{8}-\d+$') {
    $name = "$Version.7z"
    if (Test-Path -LiteralPath (Join-Path "." $name) -PathType Leaf) {
        $archive = Join-Path "." $name
    } elseif ($env:BUNDLE_URL) {
        $url = $env:BUNDLE_URL.TrimEnd('/') + "/" + $name
    } else {
        Fail "нет .\$name, и не задан BUNDLE_URL; передайте путь к архиву или URL"
    }
} else {
    Fail "$Version — не версия bundle-YYYYMMDD-N, не файл, не каталог и не URL"
}

if ((Test-Path -LiteralPath $Dest) -and (Get-ChildItem -LiteralPath $Dest -Force | Select-Object -First 1)) {
    Fail "каталог $Dest не пуст; укажите другой или удалите его"
}

if ($url) {
    # Архив кладём рядом с каталогом назначения; повторный запуск его не качает.
    $parent = Split-Path -Parent $Dest
    if (-not $parent) { $parent = "." }
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $archive = Join-Path $parent $name
    if (Test-Path -LiteralPath $archive -PathType Leaf) {
        Write-Output "уже скачан: $archive (удалите файл, чтобы скачать заново)"
    } else {
        # URL не печатаем: ссылка на общую папку может нести токен доступа.
        Write-Output "скачиваю $name в $archive"
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        try {
            Invoke-WebRequest -Uri $url -OutFile "$archive.part" -UseBasicParsing
        } catch {
            Fail "скачать $name не удалось: $($_.Exception.Message)"
        }
        Move-Item -LiteralPath "$archive.part" -Destination $archive
    }
}

$sevenZip = Find-SevenZip
if (-not $sevenZip) {
    Fail "нет 7z: поставьте 7-Zip (https://7-zip.org) или добавьте 7z.exe в PATH"
}

# Распаковка во временный каталог рядом: неполный бандл не займёт место готового.
$partial = "$Dest.partial"
if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Recurse -Force }
New-Item -ItemType Directory -Force -Path $partial | Out-Null
Write-Output "распаковка $archive"
$arguments = @("x", "-y", "-bd", "-o$partial")
if ($env:BUNDLE_PASSWORD) {
    $arguments += "-p$($env:BUNDLE_PASSWORD)"
    & $sevenZip @arguments $archive | Out-Null
} else {
    & $sevenZip @arguments $archive
}
if ($LASTEXITCODE -ne 0) {
    Fail "7z не распаковал ${archive}: неверный пароль или повреждённый архив (код $LASTEXITCODE)"
}

Test-Layout $partial
Test-Manifest $partial
if (Test-Path -LiteralPath $Dest) { Remove-Item -LiteralPath $Dest -Force }
Move-Item -LiteralPath $partial -Destination $Dest
Write-Done $Dest
exit 0
