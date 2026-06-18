FROM python:3.11-slim

# System libs required by PyBullet (headless build).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager).
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy project metadata + source first for build-layer caching.
COPY pyproject.toml ./
COPY src ./src

# Install project into system Python (no venv inside container).
RUN uv pip install --system --no-cache .

# Configs at /app/configs. Override via bind mount in compose for live editing.
COPY configs ./configs

ENV CONFIGS_DIR=/app/configs
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m"]
CMD ["block_stacker.mvp0.demo", "--scenario", "all"]
