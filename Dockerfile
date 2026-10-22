# One image for both the web app (api) and the background worker.
FROM python:3.12-slim

# OCR for scanned invoices (German + English) and pdftoppm to turn PDF pages into images
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data

# Dependencies first: this layer is cached as long as pyproject.toml does not change
COPY pyproject.toml .
RUN mkdir -p app && touch app/__init__.py && pip install --no-cache-dir . && pip uninstall -y erechnung && rm -rf app

COPY app ./app
RUN useradd --create-home appuser && mkdir -p /data && chown appuser /data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
