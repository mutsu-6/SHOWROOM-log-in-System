FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /app
COPY requirements.txt .
# ベースイメージ同梱のブラウザと版を合わせる
RUN pip install --no-cache-dir -r requirements.txt "playwright==1.55.0" \
 # 付属 Chromium は H.264 非対応で SHOWROOM の映像を再生できないため Google Chrome を入れる (amd64 のみ)
 && playwright install --with-deps chrome
COPY showroom_keeper ./showroom_keeper

ENV PYTHONUNBUFFERED=1 \
    SHOWROOM_STATE_FILE=/data/storage_state.json
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "showroom_keeper"]
CMD ["run"]
