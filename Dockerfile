FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app
COPY requirements.txt .
# ベースイメージ同梱のブラウザと版を合わせる
RUN pip install --no-cache-dir -r requirements.txt "playwright==1.55.0"
COPY showroom_keeper ./showroom_keeper

ENV PYTHONUNBUFFERED=1 \
    SHOWROOM_STATE_FILE=/data/storage_state.json
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "showroom_keeper"]
CMD ["run"]
