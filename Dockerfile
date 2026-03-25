# ============================================================
# Stage 1: Install Python dependencies
# ============================================================
FROM python:3.14-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ============================================================
# Stage 2: Minimal runtime image
# ============================================================
FROM python:3.14-slim

LABEL maintainer="adrianmg231189@gmail.com"
LABEL org.opencontainers.image.source="https://github.com/devops-ia/pr-generator"
LABEL org.opencontainers.image.description="Automated PR creation from branch patterns"

# Non-root user
RUN groupadd -r prgen && useradd -r -g prgen -d /app -s /sbin/nologin prgen

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
WORKDIR /app
COPY src/ ./src/

RUN chown -R prgen:prgen /app

ENV PYTHONPATH=/app/src

USER prgen

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

EXPOSE 8080

ENTRYPOINT ["python", "-m", "pr_generator"]
