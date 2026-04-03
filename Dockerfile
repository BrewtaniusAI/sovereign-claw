FROM python:3.12-slim AS base

LABEL maintainer="Brewtanius Ink LLC"
LABEL description="Sovereign Claw — deterministic, thermodynamically governed AI agent framework"

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN pip install --no-cache-dir -e ".[all]" 2>/dev/null || pip install --no-cache-dir -e .

# Non-root user for security
RUN useradd -m -r sovereign && chown -R sovereign:sovereign /app
USER sovereign

# Default config directory
RUN mkdir -p /home/sovereign/.sovereign_claw/skills

EXPOSE 8765 8766 9090

ENTRYPOINT ["sovereign"]
CMD ["doctor"]
