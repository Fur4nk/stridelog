FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir flask flask-sqlalchemy flask-login authlib requests gunicorn

COPY app/ /app/app/
COPY templates/ /app/templates/
COPY static/ /app/static/

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--access-logfile", "-", "--chdir", "/app", "app.app:app"]
