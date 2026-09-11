FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app

COPY --chown=app:app main.py ./
COPY --chown=app:app src ./src

RUN mkdir -p /app/data && chown app:app /app/data

USER app

CMD ["python", "main.py"]
