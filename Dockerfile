FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/vibe_flipper.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vibe_flipper ./vibe_flipper
COPY seed_products.yaml seed_hardware.yaml ./
COPY scripts ./scripts

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s CMD python -m vibe_flipper.healthcheck
CMD ["uvicorn", "vibe_flipper.main:app", "--host", "0.0.0.0", "--port", "8000"]
