FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Virtual environment so any vendor CLI invoked at build time uses the same Python as the runtime.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# System dependencies. Add build-essential, git, etc. as needed for your vendor's deps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash \
 && rm -rf /var/lib/apt/lists/*

# Install runtime deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional: build-time vendor setup (Guardrails Hub validators, NVIDIA NGC, etc.)
# Uncomment and adapt if your vendor requires a build-time install step.
#
# ARG VENDOR_TOKEN
# ENV VENDOR_TOKEN=${VENDOR_TOKEN}
# COPY setup.py .
# RUN test -n "$VENDOR_TOKEN" && python setup.py

# Application code
COPY . .

# Build stamp for /debug/loaded-config
ARG BUILD_REF
ENV BUILD_REF=${BUILD_REF:-unknown}

RUN useradd --create-home --no-log-init --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
