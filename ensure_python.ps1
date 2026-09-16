# Finds a real Python on this PC (never the Microsoft Store stub) and installs one if
# there isn't one. Prints the interpreter path as its LAST line; the .bat reads that.
$ErrorActionPreference = 'Stop'

function Test-RealPython([string]$exe) {
    if (-not $exe) { return $false }
    if (-not (Test-Path $exe)) { return $false }
    if ($exe -like '*WindowsApps*') { return $false }   # Store stub: opens the Store, never runs
    try {
        $v = & $exe -c "import sys,tkinter; print('%d.%d' % sys.version_info[:2]); print(tkinter.TkVersion)" 2>$null
    } catch { return $false }
    if (-not $v) { return $false }
    $parts = @($v -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($parts.Count -lt 2) { return $false }
    $ver = [version]$parts[0]
    $tk  = [double]$parts[1]
    return ($ver -ge [version]'3.9' -and $tk -ge 8.6)
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:GAMEASSETHARVESTER_BUILD_PYTHON) { $candidates.Add($env:GAMEASSETHARVESTER_BUILD_PYTHON) }
    foreach ($tag in '3.14','3.13','3.12','3.11','3.10','3.9') {
        try {
            $p = & py -$tag -c "import sys; print(sys.executable)" 2>$null
            if ($p) { $candidates.Add($p.Trim()) }
        } catch { }
    }
    foreach ($name in 'python.exe','python3.exe') {
        Get-Command $name -ErrorAction SilentlyContinue | ForEach-Object { $candidates.Add($_.Source) }
    }
    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*", "C:\Python*")) {
        Get-ChildItem -Path $root -Filter python.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates.Add($_.FullName) }
    }
    foreach ($c in $candidates) { if (Test-RealPython $c) { return $c } }
    return $null
}

$py = Find-Python
if ($py) { Write-Output $py; exit 0 }

Write-Host "No suitable Python found - installing one."
$installed = $false
if (Get-Command winget -ErrorAction SilentlyContinue) {
    try {
        winget install --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements | Out-Host
        $installed = $true
    } catch { Write-Host "winget install failed: $_" }
}
if (-not $installed) {
    $url = 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe'
    $tmp = Join-Path $env:TEMP 'python-installer.exe'
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    Start-Process -FilePath $tmp -ArgumentList @(
        '/quiet','InstallAllUsers=0','PrependPath=1','Include_tcltk=1','Include_pip=1'
    ) -Wait
}
$env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' +
            [Environment]::GetEnvironmentVariable('Path','Machine')
$py = Find-Python
if (-not $py) { Write-Error "Python was installed but still cannot be found. Install it manually from python.org and re-run."; exit 1 }
Write-Output $py
