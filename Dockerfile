FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

EXPOSE 10000

CMD gunicorn app:app --bind 0.0.0.0:${PORT} --timeout 120 --workers 1 --worker-class gthread --threads 8 --keep-alive 2
