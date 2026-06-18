# CLAUDE.md

積み木を積む AI の成長をリアルタイム配信するサービス。SAC（Stable-Baselines3）＋3層記憶で学習し、
PyBullet 物理シムの結果を WebSocket で Godot クライアントに配信する。

詳細は [docs/block_stacker_design.md](docs/block_stacker_design.md)（設計書）/
[docs/local_demo.md](docs/local_demo.md)（ローカル試運転）/
[docs/aws_deployment.md](docs/aws_deployment.md)（デプロイ手順）/
[docs/log_reading.md](docs/log_reading.md)（ログ解読・単語帳）。

## リポジトリ構成
- `src/block_stacker/`
  - `env/` — Gym 環境（env / tower / observation / action）
  - `policy/` — NN（HybridFeatureExtractor / Set Transformer / heightmap CNN / WeightedReplayBuffer）
  - `sim/` — PyBullet（blocks / world / carrier / heightmap）
  - `streaming/` — WebSocket 配信
  - `mvp2/` — SAC 学習＋オートカリキュラム（train.py / curriculum.py）
  - `mvp3/` — 推論・配信サーバ（ai_server.py）
- `configs/` — world / physics / reward / training の 4 YAML
- `client/` — Godot 4.4.1 (mono / C#) クライアント
- `tests/` `docs/` `deploy/` `infra-terraform/` `lambda/`

## 開発環境（重要・間違えやすい）
- **Python は `.venv\Scripts\python.exe` を直接使う**。**python.org 製 CPython 3.12**（uv 管理ではない）。
  **`uv sync` 厳禁**。`pybullet` は **numpy2 対応のソースビルド版**が必須（PyPI wheel は numpy1 ABI で壊れる）。
- テスト: `.venv\Scripts\python.exe -m pytest -q`
- Lint: `.venv\Scripts\python.exe -m ruff check src/ --select E,F,I,B,UP`（line-length 100）。
  **日本語コメント由来の RUF002/003（全角記号の ambiguous-unicode）は既存多数で無視してよい**（プロジェクトのスタイル）。
  E501 は全角を幅2換算する点に注意。
- Godot クライアントの C# ビルドには **.NET 8 SDK（x64）**が必要: `dotnet build client/block-stacker-client.csproj`。

## 実行
- 学習（カリキュラム既定 ON, Stage1→5）:
  `.venv\Scripts\python.exe -m block_stacker.mvp2.train --curriculum --n-envs 6 --total-timesteps 100000`
  - `--total-timesteps` は**全ステージ合計の上限（グローバル予算）**＝総手数はこの値以下。
  - 保存は **`output/mvp2/sac_final.zip` のみ**。途中は `output/mvp2/checkpoints/sac_<手数>_steps.zip`
    （ステージ跨ぎで連続した通算ステップ数。`tools/demo_checkpoints.ps1` がこれを参照）。
- デモ再生: `tools/demo_checkpoints.ps1`（ai_server を起動。**常に最終ステージの世界**で再生）。

## 主要な設計判断・不変条件（壊しやすいので注意）
- **卒業は2種類（OR）**: ① 散布ブロックゼロ（全ブロックを縦タワーに積み切る）で **即卒業**（高さ条件なし）。
  ② 「目標高さ到達」の成功率が直近 30 で **0.6 以上**。目標高さ = 在庫満積み高さ × ratio(0.6)。
  コンテナ環境変数 `BS_GRADUATION_RATIO/THRESHOLD/WINDOW` で上書き可。
  - **散布0 検出は positive 確認**（`len(blocks)==len(tower)`）。`find_nearest_excluding` が None を返した
    だけ（NaN/`prev_tower_ids` 陳腐化）を散布0 と誤判定しない（過去、物理破綻で最難ステージが偽卒業した不具合の対策）。
- **タワー判定 `find_tower_blocks`** は毎ステップ現在の接触グラフから再計算（履歴なし）。**縦連結（接触法線
  |z|≥0.5）のみ**。崩れて地面に落ちたブロックは縦連結が切れ散布扱いに戻る。45°斜面に乗ったものは縦連結に含む。
- **観測は「子供の狭い視野」**: per-block 枠には**近い散布ブロックの上位 `max_blocks`(=8) のみ**。積まれた
  ブロックは heightmap が山として表現。世界の合計ブロック数は 8 を超えてよい。**NaN/Inf 姿勢のブロックは観測から除外**。
- **報酬（configs/reward.yaml）**: `place_success` は **「置いた高さ」で補正**（接地横付け≈0、上段ほど満点＝案A）。
  `time_penalty=-0.05`。これは「一か所に集める／崩れた分を拾い直す」退行戦略を抑える変更。式と数値例は設計書 §報酬。
  報酬を変えたら**学習はやり直し**。
- **物理**: `contact.stiffness=40000`（角の刺さり/沈み込み対策）。貫通押し戻し（split impulse）は維持し settle で再静定。
- **デモ**: 散布0 or 物理破綻で拾える散布が無くなったら、ai_server が**全ブロックを再ランダム配置**して
  ラウンド再開（body_id 保持、MVP は演出なし）。
- **Godot 描画**: 光源は向かって右上。影は cm 級スケール向けに調整（`directional_shadow_max_distance=6` 等）。
  三角柱は ArrayMesh 手組みで**巻き順 CW（Godot は時計回りが表面）**にしないと透ける。

## 作法
- コミットや push はユーザーが明示したときだけ。既定ブランチは `main`。
- Claude Code のローカル記憶（`~/.claude/projects/.../memory/`）はこのリポジトリの外にあり、別端末/クラウドには
  引き継がれない。引き継ぎたい重要事項は本ファイルに集約する。
