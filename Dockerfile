# 土台は Playwright のイメージと同じ Ubuntu 24.04 (Python 3.12・SQLite 3.45。SQL の読み方が変わらないように)。
# Playwright のイメージは使わないブラウザ (Firefox・WebKit・画面ありの Chromium) とその部品で 4.7GB あり、
# 30GB のディスクを食っていたので、使うものだけを入れる (2026-10-21)
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python-is-python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "playwright==1.55.0" \
 # 付属 Chromium は H.264 非対応で SHOWROOM の映像を再生できないため Google Chrome を入れる (amd64 のみ)
 && playwright install --with-deps chrome \
 # Chrome が起動できない時の代わり (画面なしの Chromium だけ。keeper.launch_browser)
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /root/.cache
COPY showroom_keeper ./showroom_keeper

ENV PYTHONUNBUFFERED=1 \
    SHOWROOM_STATE_FILE=/data/storage_state.json
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "showroom_keeper"]
CMD ["run"]
