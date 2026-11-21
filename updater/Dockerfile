# updater サイドカー専用イメージ。
# docker CLI（compose プラグイン同梱）と git、python3 だけの最小構成。
# このコンテナはネットワークの受付をせず、docker.sock を持つ唯一の成分。
FROM docker:27-cli

RUN apk add --no-cache git python3

COPY updater.py /updater.py

ENTRYPOINT ["python3", "/updater.py"]
