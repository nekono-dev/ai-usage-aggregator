FROM python:3.13-slim

ARG APP_UID=10001
ARG APP_GID=10001

# 名前付きボリュームの初期所有者を、実行ユーザーに合わせる。
RUN test "$APP_UID" -gt 0 && test "$APP_GID" -gt 0 \
    && install -d -m 0750 -o "$APP_UID" -g "$APP_GID" /data

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn

COPY app/ ./app/

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
