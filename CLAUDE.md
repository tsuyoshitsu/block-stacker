# CLAUDE.md

積み木を積む AI の成長をリアルタイム配信するサービス。SAC（Stable-Baselines3）＋3層記憶で学習し、
PyBullet 物理シムの結果を WebSocket で Godot クライアントに配信する。

詳細は [docs/block_stacker_design.md](docs/block_stacker_design.md)（設計書）/
[docs/local_demo.md](docs/local_demo.md)（ローカル試運転）/
[docs/aws_deployment.md](docs/aws_deployment.md)（デプロイ手順）/
[docs/log_reading.md](docs/log_reading.md)（ログ解読・単語帳）/
[docs/tech_stack.md](docs/tech_stack.md)（使用技術一覧）/
[docs/live_mode.md](docs/live_mode.md)（ライブ配信モード）/
[docs/design_change_record.md](docs/design_change_record.md)（**過去仕様のアーカイブ**。旧記述の読み替え表）。

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
  **`uv sync` 厳禁**（この制約はローカルデモ実行 / `.venv` での開発時に限る。Lambda ビルド等の別環境での uv 使用は対象外）。`pybullet` は **numpy2 対応のソースビルド版**が必須（PyPI wheel は numpy1 ABI で壊れる）。
- テスト: `.venv\Scripts\python.exe -m pytest -q`
- Lint: `.venv\Scripts\python.exe -m ruff check src/ --select E,F,I,B,UP`（line-length 100）。
  **日本語コメント由来の RUF002/003（全角記号の ambiguous-unicode）は既存多数で無視してよい**（プロジェクトのスタイル）。
  E501 は全角を幅2換算する点に注意。
- Godot クライアントの C# ビルドには **.NET 8 SDK（x64）**が必要: `dotnet build client/block-stacker-client.csproj`。

## 実行
- 学習（カリキュラム既定 ON, Stage 1→4 を**固定ステップ数**で進行）:
  `.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6`
  - **卒業判定は廃止**。各ステージは決められたステップ数だけ走り、成績によらず次へ進む
    （旧「散布0で即卒業」は誤検出するため撤去。経緯は `docs/design_change_record.md`）。
  - ステージ予算は `configs/training.yaml` の `curriculum.stages[].steps`。既定は
    60k / 35k / 25k / 60k / 70k（Stage 1〜5, 合計 25万。既定範囲 Stage 1-4 は 18万）。
    Stage 1（ゼロから）と Stage 4/5（斜面・曲面という質的に新しい難しさ）が重い。
    **実測 2 steps/秒**なので 18万 ≈ 25時間。増やすと所要時間が比例して伸びる。
  - `--stage-steps`: 予算の上書き。単一値なら一括（`--stage-steps 100000`）、
    カンマ区切りなら実行ステージへ順に割当（`--stage-steps 60000,35000,40000`）。要素数不一致はエラー。
  - `--total-timesteps` は**全体の安全上限**。無指定なら `sac.total_timesteps`（既定 null）→
    ステージ予算の合計をそのまま使う。上限を超える分は後半ステージが切り詰められる。
  - `--target-stage`（既定 **4**）/ `--max-stage`: **最後に走るステージの上限**（厳しい方を採用）。
    卒業判定廃止に伴い「到達したら終了」ではなく単なる範囲指定。全5ステージは `--target-stage 5`。
  - 全ステージ走り切った時点でプリセットを `fresh/` に明示保存する。
  - 保存は **`output/training/fresh/sac_<YYYYMMDD-HHMMSS>_<手数>_steps.zip`**（`sac_final.zip` は廃止）。
    ソート基準は **(run_ts, steps) 昇順**（古い run → 新しい run、同 run 内はステップ昇順）。
    `find_latest_checkpoint` は最新 run の最大ステップを返す。
  - **定期 checkpoint は保存しない**。1 回の学習で `fresh/` に残るのは上記プリセット 1 本だけ。
    （途中経過を残したい場合は `--stage-steps` を刻んで複数回に分けて走らせる）
  - 学習開始時: 前回 `fresh/` に残っている checkpoint は自動で `played/` へ退避してから新規学習開始。
    `played/` には複数 run 分が蓄積されるが、ファイル名に run_ts が入るため衝突しない。
  - 学習完了時に **`output/training/replay_buffer.pkl`**（長期記憶）と **`output/training/resume_state.json`**
    が自動保存される。これらは **live_server がスナップショット引き継ぎに使う**
    （train 側の `--resume` は廃止済み。学習の再開はできない＝毎 run ゼロから）。
- **プリセット生成の標準レシピ（Stage 3 のみ・12,000 steps）**:
  `.venv\Scripts\python.exe -m block_stacker.training.train --start-stage 3 --target-stage 3 --stage-steps 12000`
  - コンセプト（不出来さを残しライブを長く楽しむ）に基づき、**わざと壁の手前で止める**。
  - 実測（2026-07-20 の中断 run）で **Stage 3 は step 12,000〜15,000 に「不器用→習得」の壁**があり、
    30,000 まで走らせると success_rate 0.97・高さ 0.607m の「上手すぎる」モデルになった。
    12,000 で止めると ep_rew はまだマイナス・success_rate 0.00・高さ 0.086m の「掴む・運ぶは
    できるが積めない子供」になる。ライブ（Stage 5）では未知形状に手こずり不出来さが残る。
  - n_envs=1 実測 **約 2.5 steps/秒**（gradient_steps=1 で学習側が軽い）→ **約 1.3 時間**。
  - **Stage 3 ゼロ開始でも学習は立ち上がる**（掴む→運ぶ→積むを土台なしで獲得できると実測確認）。
- **学習中はクライアントから見られない**（`training.train` は WebSocket を持たず、モデルも
  走破後まで書き出されない）。学習しながら見たいなら `live_server`（下記）を使う。
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
- **ステージ進行は固定ステップ制。卒業判定は存在しない**。各ステージは `stages[].steps` 分だけ走って次へ。
  - **`all_placed`（散布0）は「指標」であって進行条件ではない**。`find_tower_blocks` が返すのは
    「縦接触で連結された成分」であり「高い塔」ではないため、**横に広い低い構造でも成立する**
    （レンガ積み8個で高さ0.100m でも `len(blocks)==len(tower)` が成立することを実測確認）。
    これを卒業ゲートにしていたため Stage 1→4 が数千ステップで飛んでいた。詳細は
    `docs/design_change_record.md`。**再びゲートに使わないこと。**
  - **散布0 を達成したら `_rescatter_blocks()` で再配置してラウンドを続ける**（デモと同挙動）。
    これが無いと拾えるブロックが無いまま空振りが続き、timeout_penalty まで課されて
    **課題を完遂したエピソードが failure として記録される**。
  - `BS_GRADUATION_WINDOW`（指標の移動平均幅）と `BS_GRADUATION_RATIO`（目標高さ係数）は有効。
    `BS_GRADUATION_THRESHOLD` は**未使用**（卒業判定撤去のため残置のみ）。
  - 指標は TensorBoard の `curriculum/{success_rate, all_placed_rate, all_placed_total,
    all_placed_height, tower_height_mean}` と `rollout/ep_rew_mean`。
    **`all_placed` は必ず `all_placed_height` と併読する**（高さ非依存なので単体では
    本物の塔かレンガ積みか判別できない）。
- **学習 env は `Monitor` で包む**（`train.py:_make_env`）。これが無いと SB3 が
  `rollout/ep_rew_mean` を出せず、**報酬曲線が TensorBoard から丸ごと消える**
  （`rollout/success_rate` だけは出るので欠落に気づきにくい）。
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
