# La web mayorista (mayorista.tribuneros.store). Se construye desde la carpeta
# `mayorista/`: ver el servicio `mayorista` en ../docker-compose.yml.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# curl es para el health check del panel (la imagen slim no lo trae).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ ./static/

RUN useradd --create-home mayorista && chown -R mayorista:mayorista /app
USER mayorista

ENV PUERTO=8000
EXPOSE 8000

CMD ["python", "sitio.py"]
