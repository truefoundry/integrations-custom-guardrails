FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Virtual environment so any vendor CLI invoked at build time uses the same Python as the runtime.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# System dependencies. build-essential is required by torch's CPU wheel on slim;
# git is required because some Weave deps still pull from git tags.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash git build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install runtime deps (weave[scorers] pulls transformers + torch ~ 1.5 GB).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install uv && uv pip install -r requirements.txt

# Pre-bake the Celadon model into the image so cold-start is fast and there's
# no first-request stall. Cache lives in /root/.cache/wandb/weave-scorers/.
# Adds ~6 min to image build time, ~550 MB to image size; eliminates the
# equivalent stall on every pod restart.
#
# HF_HUB_DISABLE_PROGRESS_BARS keeps the build log readable.
# TRANSFORMERS_VERBOSITY=error suppresses the cosmetic
# "Do you wish to run the custom code?" line printed by transformers even
# when trust_remote_code=True is set programmatically (which it is in the
# scorer's load_model).
ENV HF_HUB_DISABLE_PROGRESS_BARS=1
ENV TRANSFORMERS_VERBOSITY=error
RUN python -c "from weave.scorers import WeaveToxicityScorerV1; WeaveToxicityScorerV1()"

# Application code
COPY . .

# Build stamp for /debug/loaded-config
ARG BUILD_REF
ENV BUILD_REF=${BUILD_REF:-unknown}

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
