FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Virtual environment so any vendor CLI invoked at build time uses the same Python as the runtime.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# System dependencies. Onyx is a thin HTTP client — no git / build-essential needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash \
 && rm -rf /var/lib/apt/lists/*

# Install runtime deps
COPY requirements.txt .
RUN pip install --upgrade pip && pip install uv && uv pip install -r requirements.txt

# Application code
COPY . .

# Build stamp for /debug/loaded-config
ARG BUILD_REF
ENV BUILD_REF=${BUILD_REF:-unknown}

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
