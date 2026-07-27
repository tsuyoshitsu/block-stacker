# block-stacker のコンテナイメージ（live / learner の 2 ターゲット）。
#
# 両者は**まったく同じ依存**を必要とする。live_server は「配信しながらバックグラウンドで
# SAC を学習する」ため、推論だけでなく学習側の依存（stable-baselines3 / torch /
# tensorboard）を丸ごと使うからである。依存リストを 2 箇所に書くと必ず drift するので、
# 共通の `base` ステージを 1 つ置き、そこから live / learner を分岐させる multi-stage 構成にした。
# （旧 `Dockerfile.learner` は本ファイルの `learner` ターゲットに統合済み）
#
# ビルド:
#   docker build -t block-stacker/live:latest .                      # live（既定ターゲット）
#   docker build --target learner -t block-stacker/learner:latest .   # learner
#
# 設計上のポイント:
#   - 本プロジェクトの NN は小規模 (HybridFeatureExtractor + MLP, 〜十万 params) かつ
#     PyBullet 物理シムが CPU bound のため、GPU の利点が出ない。
#     → CPU torch wheel に切替し、g4dn (GPU) よりインスタンス単価を 40% 削減。
#   - PyBullet headless でも libGL.so 等は必要なので OpenGL ライブラリは残す。
#   - 描画は Godot クライアント側で行うので、イメージにグラフィックスタックは要らない。

# ---------------------------------------------------------------------------
# base: live / learner 共通の依存とアプリコード
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) torch (CPU wheel) を専用 index から先に取得。
RUN pip install --no-cache-dir \
        torch --index-url https://download.pytorch.org/whl/cpu

# 2) 非 torch 依存を PyPI から (--no-deps で torch 上書き防止)
RUN pip install --no-cache-dir \
        "pybullet>=3.2.6" \
        "numpy>=1.26" \
        "pyyaml>=6.0" \
        "gymnasium>=0.29" \
        "stable-baselines3>=2.3.0" \
        "tensorboard>=2.14" \
        "websockets>=12.0"

# 3) プロジェクトコードを deps 再解決せずにインストール
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# configs は EC2 ホストから volume mount する想定 (S3 から sync 済)。
# イメージにも fallback として同梱しておく。
COPY configs ./configs

ENV CONFIGS_DIR=/app/configs

ENTRYPOINT ["python", "-m"]

# ---------------------------------------------------------------------------
# learner: 月初のプリセット生成バッチ (c6a.4xlarge / AMD EPYC, CPU-only)
# ---------------------------------------------------------------------------
FROM base AS learner

# カリキュラム関連はコンテナ環境変数で上書きできる（優先順位: env var > training.yaml > 既定値）。
#   BS_GRADUATION_RATIO   目標高さ = 在庫満積み高さ × ratio（既定 0.6）
#   BS_GRADUATION_WINDOW  指標の移動平均を取る直近エピソード数（既定 30）
# 名前に graduation が残っているが**卒業判定は無い**。指標の集計方法にしか効かない。
# 例: docker run -e BS_GRADUATION_RATIO=0.7 ... block-stacker/learner

# 既定は**プリセット生成**（train の既定がそのまま Stage 3 のみ・5,000 steps）。
#   月初に 1 回走らせてその月のシードモデルを作る用途（docs/aws_deployment.md §5.3）。
#   **卒業判定は廃止済み**。各ステージは configs/training.yaml の stages[].steps 分だけ走り、
#   成績によらず次へ進む（既定は Stage 3 のみなので進行自体が起きない）。
#   n_envs は configs の sac.n_envs（既定 1）を使う。gradient_steps と揃える必要があるため
#   ここでは上書きしない。
#   フルカリキュラムを回すなら CMD を上書き: --start-stage 1 --target-stage 4
# 保存: 走破後に fresh/sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip を **1 本だけ**保存する
#   （定期 checkpoint は撤去済み。sac_final.zip も廃止）。
CMD ["block_stacker.training.train"]

# ---------------------------------------------------------------------------
# live: 配信＋バックグラウンド学習の本番機 (c6a.2xlarge)
#       ※ --target 省略時のビルド対象（最終ステージ）
# ---------------------------------------------------------------------------
FROM base AS live

# WebSocket 配信ポート。streamer EC2 の Caddy がここへ reverse_proxy する。
EXPOSE 8765

# ボリューム想定（deploy/userdata/live.sh の docker run と一致させること）:
#   /app/models  スナップショット（モデル zip / replay_buffer.pkl / resume_state.json）の
#                読み書き先。前営業日の状態をここから自動で引き継ぐ。
#   /app/configs S3 から sync した configs。
#   /app/state   world_state（現状 live_server は未使用。将来用に mount だけしてある）
#
# --n-envs / --sync-every は既定（1 / 50）がそのまま運用値なので指定しない。
#   n_envs はスナップショット作成時と一致必須（不一致だと学習スレッドだけ落ちて配信は続く）。
# 初回起動のみ `--no-resume` を足してスナップショットを無視する。
CMD ["block_stacker.serving.live_server", \
     "--snapshot-dir", "/app/models", \
     "--configs-dir", "/app/configs", \
     "--host", "0.0.0.0", \
     "--port", "8765"]
