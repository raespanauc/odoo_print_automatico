FROM python:3.12-slim

# pdftoppm para convertir PDF a imagen
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir requests python-dotenv loguru Pillow

COPY config.py odoo_client.py printer.py monitor.py ./

CMD ["python", "monitor.py"]
