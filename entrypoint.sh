#!/bin/bash
set -e

# Iniciar CUPS en background
echo "Iniciando CUPS..."
cupsd

# Esperar a que CUPS esté listo
sleep 2

# Iniciar el monitor
echo "Iniciando OdooPrintMonitor..."
exec python monitor.py
