FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl wget ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY advisor /app/advisor

# Persistent data directory — mount this as a volume in Coolify
RUN mkdir -p /data/advisor/data/cache \
             /data/advisor/ml_filter/models \
             /data/advisor/hyperopt/results \
             /data/advisor/report/output \
             /data/db

VOLUME ["/data"]

# Store SQLite DB in /data/db so it survives redeployments
ENV DATABASE_URL=sqlite:////data/db/paper_trading_v2.db

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
