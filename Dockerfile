FROM python:3.12-slim AS base

LABEL maintainer="BrewtaniusAI" \
      description="Sovereign Claw — Governed Sovereign Agent Runtime" \
      version="3.1.0"

WORKDIR /app

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir -e ".[dev]"

# Non-root user for security
RUN useradd -m -r sovereign && chown -R sovereign:sovereign /app
USER sovereign

# Default config directory
RUN mkdir -p /home/sovereign/.sovereign_claw/skills

EXPOSE 8765 8766 9090

ENTRYPOINT ["python", "-m", "sovereign_claw.cli"]
CMD ["run"]
