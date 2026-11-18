FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py .

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3)" "http://127.0.0.1:${PORT:-8000}/health" || exit 1
CMD ["python", "server.py"]
