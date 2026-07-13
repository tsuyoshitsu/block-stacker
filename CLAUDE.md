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
  - `training/` — SAC 学習＋オートカリキュラム（train.py / curriculum.py）
  - `serving/` — 推論・配信サーバ（ai_server.py / live_server.py）
- `configs/` — world / physics / reward / training の 4 YAML
- `client/` — Godot 4.4.1 (mono / C#) クライアント
- `tools/` — 運用スクリプト（demo_checkpoints.ps1 / local_loop.ps1 / advance_day.ps1）
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
- 学習（カリキュラム既定 ON, Stage 1→4 卒業で打ち切り）:
  `.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 4000`
  - `--total-timesteps` は**安全上限（タイムアウト）**。`--target-stage` 到達前に使い切ると budget 打ち切り。
  - `--target-stage`（既定 **4**）: 指定ステージを卒業した時点で学習終了し、プリセットを `fresh/` に保存。
    全ステージ（Stage 5 = 全形状）まで走る場合は `--target-stage 5`。
    budget 打ち切りまで走り続ける従来動作は `--target-stage 9999` 等（到達しない値）で再現。
  - 保存は **`output/training/fresh/sac_<YYYYMMDD-HHMMSS>_<手数>_steps.zip`**（`sac_final.zip` は廃止）。
    ソート基準は **(run_ts, steps) 昇順**（古い run → 新しい run、同 run 内はステップ昇順）。
    `find_latest_checkpoint` は最新 run の最大ステップを返す。
  - checkpoint は **`checkpoint_every`（既定 50000）ステップ間隔**で定期保存される
    （`configs/training.yaml` の `sac.checkpoint_every` で変更可）。
    `save_freq = checkpoint_every // n_envs`（n_calls 基準）で算出。total_timesteps に依存しない。
    卒業時には追加で明示的 checkpoint も保存（周期と合致しない場合の補完）。
  - 学習開始時: 前回 `fresh/` に残っている checkpoint は自動で `played/` へ退避してから新規学習開始。
    `played/` には複数 run 分が蓄積されるが、ファイル名に run_ts が入るため衝突しない。
  - 学習完了時に **`output/training/replay_buffer.pkl`**（長期記憶）と **`output/training/resume_state.json`**
    が自動保存される。`resume_state.json` には `num_timesteps`, `next_stage_id`, `completed_stages`,
    `timestamp` が格納され、次回 `--resume` 時に参照される。
- 前回の学習を引き継いで再開（`--resume`）:
  `.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 4000 --resume`
  - **勘（NN重み）**: `find_latest_checkpoint` で最新 run の最大ステップ checkpoint を自動選択して `SAC.load()`（無加工）。
  - **長期記憶**: `replay_buffer.pkl` を復元し、`resume_state.json` の `timestamp` から経過日数を自動算出して
    `global_step += 経過日数 × steps_per_day`（既定 5000）の時間減衰を適用する。
    `configs/training.yaml` の `resume.elapsed_days` / `resume.elapsed_steps` で手動上書き可。
  - **短期記憶**: 引き継がない（`env.reset()` で自動クリア、設計通り）。
  - カリキュラム進捗（`next_stage_id`）を引き継ぎ、前回の続きのステージから再開。
- 日次配信モード（fresh/played 方式）:
  - `tools/advance_day.ps1`（非ブロッキング）: `fresh/` 最古モデルで ai_server 起動 → 前回モデルを `played/` へ退避
  - `fresh/` が空になったら `played/` の最大ステップモデルを繰り返し再生（フォールバック）
  - `advance_day.ps1 -DurationSeconds <秒>`: ai_server を指定秒数で自動終了させる。
  - `advance_day.ps1 -DryRun`: 表示のみ（ai_server 起動・移動なし）
- ローカル成長観察（開発用）: `tools/local_loop.ps1`（`fresh/` を (run_ts, steps) 昇順に1巡再生して終了。`played/` 移動なし）。
- デモ再生（手動・開発用）: `tools/demo_checkpoints.ps1`（ai_server を起動。**常に最終ステージの世界**で再生）。
- ライブ配信モード（訓練＋配信融合）:
  `.venv\Scripts\python.exe -m block_stacker.serving.live_server --snapshot-dir output/training --duration 28800`
  - 常に最終ステージ（Stage 5: 全4形状・最難）で配信する。
  - `--duration`（秒, default: 28800 = 8h）: 0 で無制限。
  - `--model <path>`: モデルを明示（無指定なら `--snapshot-dir` から自動選択）。
  - **Step B 以降で有効になるオプション**: `--n-envs`（バックグラウンド学習並列数）,
    `--sync-every`（重み同期間隔）, `--no-resume`（スナップショット無視・初回起動用）。
  - ライブモードのスナップショット読み書き先は `--snapshot-dir`（既定 `output/training`）と共通。

## 主要な設計判断・不変条件（壊しやすいので注意）
- **卒業は2種類（OR）**: ① 散布ブロックゼロ（全ブロックを縦タワーに積み切る）で **即卒業**（高さ条件なし）。
  ② 「目標高さ到達」の成功率が直近 30 で **0.6 以上**。目標高さ = 在庫満積み高さ × ratio(0.6)。
  コンテナ環境変数 `BS_GRADUATION_RATIO/THRESHOLD/WINDOW` で上書き可。
  - **散布0 検出は positive 確認**（`len(blocks)==len(tower)`）。`find_nearest_excluding` が None を返した
    だけ（NaN/`prev_tower_ids` 陳腐化）を散布0 と誤判定しない（過去、物理破綻で最難ステージが偽卒業した不具合の対策）。
  - **最終ステージ卒業後も予算が残っていれば継続**（`train.py` ループ後の post-loop ブロック）。
    `GraduationCallback` が `model.learn()` を早期終了させるため最終ステージ卒業時に checkpoint が
    欠落するのを防ぐ。`reset_num_timesteps=False` で通算ステップ数を引き継ぎ、最終ステージ環境で
    `total_timesteps` まで走り切る。
- **タワー判定 `find_tower_blocks`** は毎ステップ現在の接触グラフから再計算（履歴なし）。**縦連結（接触法線
  |z|≥0.5）のみ**。崩れて地面に落ちたブロックは縦連結が切れ散布扱いに戻る。45°斜面に乗ったものは縦連結に含む。
- **観測は「子供の狭い視野」**: per-block 枠には**近い散布ブロックの上位 `max_blocks`(=8) のみ**。積まれた
  ブロックは heightmap が山として表現。世界の合計ブロック数は 8 を超えてよい。**NaN/Inf 姿勢のブロックは観測から除外**。
- **報酬（configs/reward.yaml）**: `place_success` は **「置いた高さ」で補正**（接地横付け≈0、上段ほど満点＝案A）。
  `time_penalty=-0.05`。これは「一か所に集める／崩れた分を拾い直す」退行戦略を抑える変更。式と数値例は設計書 §報酬。
  報酬を変えたら**学習はやり直し**。
- **物理**: `contact.stiffness=40000`（角の刺さり/沈み込み対策）。`friction.block_to_block=0.45`（0.6→×0.75: ブロック間固着緩和）。貫通押し戻し（split impulse）は維持し settle で再静定。
- **デモ**: 散布0 or 物理破綻で拾える散布が無くなったら、ai_server が**全ブロックを再ランダム配置**して
  ラウンド再開（body_id 保持、MVP は演出なし）。
- **Godot 描画**: 光源は向かって右上。影は cm 級スケール向けに調整（`directional_shadow_max_distance=6` 等）。
  三角柱は ArrayMesh 手組みで**巻き順 CW（Godot は時計回りが表面）**にしないと透ける。

## 作法
- コミットや push はユーザーが明示したときだけ。既定ブランチは `main`。
- Claude Code のローカル記憶（`~/.claude/projects/.../memory/`）はこのリポジトリの外にあり、別端末/クラウドには
  引き継がれない。引き継ぎたい重要事項は本ファイルに集約する。
