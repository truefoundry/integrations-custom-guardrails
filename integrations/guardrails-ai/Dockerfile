# Use Python 3.11 slim image as base
FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Virtual environment so the guardrails CLI invoked at build time uses the
# same Python as the runtime app.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# System dependencies. git is required because guardrails-ai's PyPI package is
# currently quarantined and we pin to a GitHub tag in requirements.txt;
# build-essential is for C-extension validator deps; curl + bash convenience.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash git build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install runtime deps
COPY requirements.txt .
RUN pip install --upgrade pip && pip install uv && uv pip install -r requirements.txt

# Hub validator install at build time. GUARDRAILS_TOKEN comes from the
# TFY secret reference passed as a Docker build arg via deploy.py
# (see deploy.py build_args). The token is consumed by `guardrails configure`
# during this layer and is not present at runtime.
ARG GUARDRAILS_TOKEN
ENV GUARDRAILS_TOKEN=${GUARDRAILS_TOKEN}
COPY setup.py .
RUN test -n "$GUARDRAILS_TOKEN" && python setup.py

# Application code
COPY . .

# Build stamp for /debug/loaded-config
ARG BUILD_REF
ENV BUILD_REF=${BUILD_REF:-unknown}

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
