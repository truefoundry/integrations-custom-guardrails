FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install uv && uv pip install -r requirements.txt

COPY . .

# Build stamp for /debug/loaded-config
ARG BUILD_REF
ENV BUILD_REF=${BUILD_REF:-unknown}

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
