FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system signalstack && \
    useradd --system --gid signalstack --home-dir /app --shell /usr/sbin/nologin signalstack && \
    mkdir -p /app/data/uploads /app/reports && \
    chown -R signalstack:signalstack /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY app.py ./app.py
COPY scripts/run_streamlit.py ./scripts/run_streamlit.py

RUN python -m pip install --no-cache-dir .

USER signalstack
EXPOSE 8501

CMD ["python", "scripts/run_streamlit.py"]
