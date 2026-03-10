# install_service.ps1 — Instala OdooPrintMonitor como servicio Windows via NSSM
#Requires -RunAsAdministrator

$ServiceName = "OdooPrintMonitor"
$ProjectDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe   = Join-Path $ProjectDir "venv\Scripts\python.exe"
$ScriptPath  = Join-Path $ProjectDir "monitor.py"
$LogDir      = Join-Path $ProjectDir "logs"

# Crear directorio de logs
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# Verificar que NSSM esta disponible
if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Error "NSSM no encontrado. Instalalo desde https://nssm.cc o via: choco install nssm"
    exit 1
}

# Verificar entorno virtual
if (-not (Test-Path $PythonExe)) {
    Write-Error "No se encontro el entorno virtual en $PythonExe. Crea uno con: python -m venv venv"
    exit 1
}

# Detener y eliminar servicio previo si existe
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Eliminando servicio previo..."
    nssm stop $ServiceName
    nssm remove $ServiceName confirm
}

# Instalar servicio
Write-Host "Instalando servicio $ServiceName..."
nssm install $ServiceName $PythonExe
nssm set $ServiceName AppParameters $ScriptPath
nssm set $ServiceName AppDirectory $ProjectDir
nssm set $ServiceName AppStdout (Join-Path $LogDir "service.log")
nssm set $ServiceName AppStderr (Join-Path $LogDir "service_error.log")
nssm set $ServiceName Start SERVICE_AUTO_START
nssm set $ServiceName Description "Monitorea albaranes Odoo y los imprime automaticamente"

# Iniciar servicio
Write-Host "Iniciando servicio..."
Start-Service $ServiceName
$svc = Get-Service $ServiceName
Write-Host "Servicio '$ServiceName' estado: $($svc.Status)"
