FROM python:3.12-slim

# pdftoppm para convertir PDF a imagen
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir requests python-dotenv loguru Pillow flask

COPY config.py odoo_client.py printer.py monitor.py print_store.py dashboard.py ./
COPY templates/ templates/

RUN mkdir -p /app/data

EXPOSE 5000

CMD ["python", "monitor.py"]
