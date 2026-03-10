FROM python:3.12-slim

# Instalar CUPS para imprimir PDFs a impresoras de red
RUN apt-get update && apt-get install -y --no-install-recommends \
    cups \
    cups-client \
    cups-filters \
    && rm -rf /var/lib/apt/lists/*

# Configurar CUPS para aceptar comandos locales sin autenticación
RUN sed -i 's/Listen localhost:631/Listen 0.0.0.0:631/' /etc/cups/cupsd.conf && \
    echo "ServerAlias *" >> /etc/cups/cupsd.conf

WORKDIR /app

RUN pip install --no-cache-dir requests python-dotenv loguru

COPY config.py odoo_client.py printer.py monitor.py ./

# Script de inicio: arranca CUPS y luego el monitor
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
