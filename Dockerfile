FROM python:3.12-alpine AS builder

RUN pip install --no-cache-dir --prefix=/install \
    flask flask-sqlalchemy flask-login authlib requests gunicorn

FROM python:3.12-alpine

COPY --from=builder /install /usr/local

WORKDIR /app

COPY app/ /app/app/
COPY templates/ /app/templates/
COPY static/ /app/static/

RUN adduser -D -u 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--access-logfile", "-", "--chdir", "/app", "app.app:app"]
