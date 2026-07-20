# ローカル試運転手順書

ローカルで SAC を学習させ、生成したモデルをデモ再生して動作を確認するためのガイド。

> AWS にデプロイする本番は別途 [`docs/aws_deployment.md`](aws_deployment.md) を参照。
> こちらは試運転・動作確認用のローカルワークフロー。

## 概要

```
┌──────────────────────────────────────────────────────────┐
│ 1. 学習を回す                                            │
│    python -m block_stacker.training.train --total-timesteps 2000000 │
│    → output/training/fresh/ に sac_20260713-100000_198000_steps.zip │
└──────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────┐
│ 2. Godot クライアントを起動                               │
│    Godot で client/scenes/main.tscn を再生              │
│    → 「サーバとの通信を試行中...」表示                   │
└──────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────┐
│ 3. モデルをデモ再生                                      │
│    tools\demo_checkpoints.ps1                            │
│    → ai_server が最終ステージの世界でモデルを動かす      │
└──────────────────────────────────────────────────────────┘
                            │
                            ▼
            AI がどこまで積めるかを目視確認
```

## 前提

- **Python 環境**: `.venv` は **python.org の素の CPython 3.12**（`C:\Users\iii03\AppData\Local\Programs\Python\Python312`）で作成済み。**uv 管理 python ではない**ので `uv sync` で作り直さないこと（理由と再構築手順は下の「環境メモ」）。
- Godot 4.4.1 .NET 版インストール済み (`D:\Godot_v4.4.1-stable_mono_win64\`)
- C# プロジェクトビルド済み（.NET 版 Godot は初回再生時に自動ビルドするので必須ではない。明示するなら `dotnet build client/block-stacker-client.csproj`）

> **環境メモ（重要）**: かつて `.venv` は uv 管理 python に紐づいていたが、対話シェルから
> その python に到達できず全コマンドが `No Python at '...'` で落ちた。Anaconda で作り直すと
> 今度は古い VC ランタイムで torch の `c10.dll` 初期化が失敗（`WinError 1114`）。最終的に
> **素の python.org 3.12** で作り直して解決済み。pybullet は numpy 2 対応ビルドが必須
> （PyPI 配布ホイールは `numpy.core.multiarray failed to import` になる）。
>
> 万一 `.venv` を作り直す必要が出たら（uv は使わない。`py -3.12` は Anaconda を指す
> ことがあるので**明示パス**で python.org の python を使う）:
> ```powershell
> Rename-Item .venv .venv_old
> & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
> .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
> # pybullet が numpy2 ABI 不一致なら、numpy2 対応 .pyd を旧 venv からコピー
> ```

## 手順

### Step 1: 学習を実行

**既定でオートカリキュラム（Stage 1→4 を自動進行）**。`--total-timesteps` は安全上限（タイムアウト）で、
**`--target-stage`（既定 **4**）で指定したステージを卒業した時点で学習終了・プリセット保存**する。
卒業条件：①散布0（全ブロックを縦タワーに積み切る）で即卒業、または ②目標高さ到達の成功率が 0.6（直近30エピソード）。
目標高さ＝在庫満積み高さ×`ratio`（既定 0.6）。

> **`--target-stage` と段階別フロー**:
> - `--target-stage 4`（既定）: Stage 4 卒業で終了。`fresh/` にプリセット保存。Stage 5（円柱追加）は実行しない。
> - `--target-stage 5`: Stage 5（全形状）まで完走。より完成度の高いプリセット。
> - `--target-stage 9999`: 指定ステージが存在しないため budget 打ち切りまで走り切る（旧来の全ステージ完走相当）。
> - `--total-timesteps` で指定した上限に先に達すると、`--target-stage` 未到達でも終了（budget 打ち切り）。

```powershell
# ---- プリセット生成（既定: Stage 4 卒業で終了）----
# total-timesteps は「Stage 4 を卒業できなかった場合の安全上限」（目安 1M〜5M）
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4 --total-timesteps 2000000

# Stage 5（全形状、円柱含む）まで完走したい場合
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4 --total-timesteps 5000000 --target-stage 5

# まず Stage 1→2 だけ試す（動作確認用。--target-stage で止めないと Stage 2 卒業後も予算を使い切るまで走る）
.venv\Scripts\python.exe -m block_stacker.training.train --target-stage 2 --n-envs 4 --total-timesteps 2000000

# 単一ステージだけ素早く確認したいとき（Stage 1 のみ）
.venv\Scripts\python.exe -m block_stacker.training.train --no-curriculum --n-envs 4 --total-timesteps 50000

# 動作確認だけ（ステージ卒業なし、数秒で終わる超短縮版）
.venv\Scripts\python.exe -m block_stacker.training.train --no-curriculum --n-envs 2 --total-timesteps 500
```
> 新人 AI は短い予算だと卒業条件に届かず「卒業せず中断」になる。進行そのものを素早く見たいだけなら
> `configs/training.yaml` の `graduation.threshold` を下げる／`ratio` を下げる（目標を低く）。
> これらは **環境変数 `BS_GRADUATION_THRESHOLD` / `BS_GRADUATION_RATIO` / `BS_GRADUATION_WINDOW`**
> でも上書きできる（env var > training.yaml > 既定）。本番（AWS の learner）も既定でカリキュラム ON。

学習中：
- **通常は `fresh/` に卒業プリセット 1 本**（`sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip`）が生成される。
  `--target-stage`（既定 4）卒業時に明示保存されるモデルで、これが最終モデル（`sac_final.zip` は廃止）。
- 定期 checkpoint も **`checkpoint_every`（既定 50000）ステップ間隔**で保存される仕組みだが、
  卒業が 50,000 step 未満で起きると**一度も発火しない**ため、実際には**卒業プリセット 1 本だけ**になることが多い。
  卒業まで 50k step を超える長い run では途中 checkpoint も併せて残る。
  先頭の日時（`run_ts`）は同一 run で共通。間隔は `configs/training.yaml` の `sac.checkpoint_every` で変更可。
- `output/training/tb/` に TensorBoard ログが書かれる
- `output/training/replay_buffer.pkl`（長期記憶）と `output/training/resume_state.json` が**毎回**保存される（次回 `--resume` で利用）
- **前回の学習の `fresh/` が残っている場合**: 学習開始時に自動で `played/` へ退避してから新しいモデルを `fresh/` に生成する

#### 前回の学習を引き継ぐ（--resume）

前回の学習が終わった後、続きから学習を再開できる。**勘（NN重み）** と **長期記憶（replay buffer）**
を引き継ぎ、長期記憶には「前回終了からの経過日数×5000 step 分の時間減衰」を自動適用する。

```powershell
# 初回学習を済ませてから続きを学習（find_latest_checkpoint で最新 run の最大ステップ checkpoint を自動選択）
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 2000000 --resume
```

- **経過日数は自動算出**（`resume_state.json` の `timestamp` から現在時刻との差を計算）。
- 減衰の強さを調整したいときは `configs/training.yaml` の `resume.steps_per_day`（既定 5000）を変更。
- テスト用に手動で経過日数を指定するには `resume.elapsed_days` を設定するか、
  `resume_state.json` の `timestamp` を書き換える。
- カリキュラム進捗も引き継がれ、`resume_state.json` の `next_stage_id` から再開する。

別ターミナルで TensorBoard を起動すると学習曲線がリアルタイムで見られる：

```powershell
.venv\Scripts\python.exe -m tensorboard.main --logdir output\training\tb
# ブラウザで http://localhost:6006 を開く
```

### Step 2: Godot クライアントを起動

学習中でも学習後でも OK。

```powershell
& "D:\Godot_v4.4.1-stable_mono_win64\Godot_v4.4.1-stable_mono_win64.exe" `
    --path C:\Users\iii03\block-stacker\client res://scenes/main.tscn
```

サーバ未起動なので「**サーバとの通信を試行中**...」と表示される。

### Step 3: モデルを再生

[`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1) を使うと、`fresh/` にあるモデルを
一覧表示して選択再生できる（1 本しかない場合はそれを選ぶだけ）。

> **デモは常に最終ステージ（全形状）でモデルを動かす**（`ai_server` 既定）。`--model` 無指定なら
> `fresh/` または `played/` の最大ステップ checkpoint を自動選択。特定ステージの
> 世界で見たいなら `ai_server --stage N` を使う。
>
> **散布ブロックゼロになったら自動で仕切り直す**: 全ブロックを積み切る（または物理破綻で拾える
> 散布ブロックが無くなる）と、`ai_server` は**全ブロックを再ランダム配置**してラウンドを再開する
> （body_id は保持するので配信は途切れない）。

```powershell
tools\demo_checkpoints.ps1
```

出力例（Stage 4 卒業プリセット 1 本の場合）：

```
発見された checkpoint (1 件):
  0: 20260713-100000 / 198000 steps  (sac_20260713-100000_198000_steps.zip)

番号を入力 (例: 0)、'all' で全部、'q' で終了
> 0
=== 20260713-100000 / 198000 steps (sac_20260713-100000_198000_steps.zip) ===
  ai_server PID 12345、 60 秒間再生...
```

`ai_server` を直接起動してもよい（`--model` 無指定で `fresh/` / `played/` の最大ステップを自動選択）：

```powershell
.venv\Scripts\python.exe -m block_stacker.serving.ai_server --host 127.0.0.1
```

`played/` に退避済みのモデルを再生したいときは
`demo_checkpoints.ps1 -CheckpointsDir output\training\played` を使う。

→ Godot 画面で AI の動きを観察できる。

### Step 4: 観察する

再生中に見るポイント：

| 観察ポイント | 学習が足りないとき | 学習が進んだとき |
|----------|------|------|
| ブロックを掴むか | 失敗が多い | ほぼ成功 |
| タワーの近くに置くか | 関係ない場所に置く | 真上に置く |
| タワー崩落 | 頻繁 | ほぼなし |
| 形状選択 | ランダム | 安定する形状を優先 |

Stage 1 しか学習していないモデルを最終ステージの世界（円柱あり）で再生すると当然うまく積めない。
`--target-stage 4`（既定）以上まで学習したモデルで確認すること。

## ヘルパースクリプトの引数

### `tools\demo_checkpoints.ps1`（モデルを選んで再生）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-CheckpointsDir` | `output\training\fresh` | checkpoint ディレクトリ（fresh/ または played/ を指定） |
| `-Seconds` | 60 | 各 checkpoint の再生時間（秒）|
| `-Mode` | `interactive` | `interactive` (一つ選ぶ) または `auto` (見つかった順に全部) |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-Godot` | `D:\Godot_...\Godot_...exe` | Godot 実行パス |
| `-LaunchGodot` | (未指定なら手動) | このフラグで Godot を自動起動 |

### `tools\local_loop.ps1`（`fresh/` を1巡再生して終了）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-Dir` | `output\training\fresh` | checkpoint ディレクトリ |
| `-SwitchSeconds` | `60` | 1 モデルあたりの再生秒数 |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-AiHost` | `127.0.0.1` | ai_server の listen ホスト |
| `-AiPort` | `8765` | ai_server の listen ポート |

### `tools\advance_day.ps1`（日次 ai_server 切り替え）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-FreshDir` | `output\training\fresh` | 新しい checkpoint のディレクトリ |
| `-PlayedDir` | `output\training\played` | 再生済み checkpoint の退避先 |
| `-StateFile` | `output\training\advance_state.json` | 前回状態の記録ファイル |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-AiHost` | `127.0.0.1` | ai_server の listen ホスト |
| `-AiPort` | `8765` | ai_server の listen ポート |
| `-DurationSeconds` | `0` (無制限) | 0 以外なら ai_server に `--duration` を渡す |
| `-DryRun` | (なし) | 表示のみ、ai_server は起動しない |

### `ai_server`（推論・配信サーバ）の主要オプション

| オプション | デフォルト | 説明 |
|----------|---------|------|
| `--model` | 自動選択 | モデルパス（未指定: `fresh/` / `played/` の最大ステップ checkpoint） |
| `--host` | `0.0.0.0` | listen ホスト |
| `--port` | `8765` | listen ポート |
| `--stage` | 最終ステージ | デモするステージ番号 |
| `--duration` | `0` (無制限) | 再生秒数、0 なら常駐（`advance_day.ps1 -DurationSeconds` から渡される） |
| `--thinking-pause` | `2.0` | AI が次手を考える間隔（秒） |
| `--settle-seconds` | `1.5` | 設置後の物理安定待ち時間（秒） |

## テスト・静的チェック

```powershell
# 全テストを実行
.venv\Scripts\python.exe -m pytest -q

# ruff lint（E,F,I,B,UP を対象。日本語コメント由来の RUF002/003 は無視してよい）
.venv\Scripts\python.exe -m ruff check src/ --select E,F,I,B,UP
```

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| `No Python at '...uv\python\...'` | `.venv` が壊れている（uv 管理 python に到達不能）。素の python.org 3.12 で作り直す（上の「環境メモ」）。`uv sync` で直そうとしない |
| `WinError 1114`（`c10.dll`） | Anaconda 由来の venv で torch が落ちている。素の python.org 3.12 で作り直す |
| `numpy.core.multiarray failed to import` | pybullet が numpy 1 ABI の配布ホイール。`pip install --no-binary pybullet --force-reinstall pybullet==3.2.7`（要 MSVC）か、numpy2 対応ビルドの `.pyd` を使う |
| `checkpoints ディレクトリが見つかりません` | 先に `training.train` を実行して学習を回す |
| `ai_server が起動直後に終了` | モデルファイル破損 / 観測形状ミスマッチ。`configs/training.yaml` を変えていないか確認 |
| Godot で AI が動かない | サーバ側ターミナルで "client connected" ログが出ているか |
| 切替時に AI 表示が一瞬止まる | 正常。WsClient が 2 秒以内に再接続するのを待つだけ |
| 途中 checkpoint で全く違う動き | 学習途中の SAC は不安定な時期がある。卒業プリセット（最大ステップ）で確認する |

## 学習時間の見積もり

i7-10750H (6 物理コア, n_envs=4) 想定。**`total_timesteps` は安全上限（タイムアウト）で、
`--target-stage 4`（既定）で Stage 4 卒業時に自動終了する**。

| 用途 | 推奨コマンド例 | 目安時間 | 出力モデル数 |
|---|---|---|---|
| 煙テスト（動作確認のみ） | `--no-curriculum --total-timesteps 500` | 数秒 | 0 |
| **Stage 4 卒業プリセット生成（既定）** | `--n-envs 4 --total-timesteps 2000000` | **ローカル: 数時間〜丸一日** | 通常 1 本（卒業プリセット） |
| Stage 5 完走（全形状） | `--n-envs 4 --total-timesteps 5000000 --target-stage 5` | ローカル: 1〜数日 | 同上 |

→ AWS **c6a.4xlarge**（n_envs=8）では Stage 4 が **1〜2h** で到達。ローカルは PyBullet
`settle_duration=2s` が律速（3 steps/sec 程度）のため長時間かかる。
出力は通常「卒業プリセット 1 本」。卒業までに 50,000 step を超えた場合のみ、
`checkpoint_every` 間隔の途中 checkpoint が加わる。

## クラウド学習との関係

このローカル workflow は「**試運転**」用。本番学習は AWS の learner EC2 が
隔週土曜 14-22（**暫定・調整中**）に走り、モデルを S3 に保存する。

ローカルで作ったモデルを AWS のデモ側で再生したい場合は S3 にアップ：

```powershell
$ACCOUNT = aws sts get-caller-identity --query Account --output text
# fresh/ の最大ステップ checkpoint を最新モデルとして S3 にアップ
$model = & .venv\Scripts\python.exe -c "from block_stacker.training.checkpoint import find_latest_checkpoint; from pathlib import Path; p = find_latest_checkpoint(Path('output/training')); print(p)"
aws s3 cp $model s3://bs-app-$ACCOUNT/models/latest.pt
```

→ 次のクラウドデモ起動時にこのモデルが自動的に読み込まれる。

---

## 日次配信モード（fresh/played 方式）

学習で生成したモデルを `fresh/` に蓄積し、**`advance_day.ps1` を毎日呼ぶだけで
古い→新しい順に自動ステップアップ**して配信する。

```
学習後           → fresh/ に sac_<run_ts>_<steps>_steps.zip（通常は卒業プリセット 1 本）が生成
advance_day.ps1  → fresh/ の最古モデルで ai_server を起動
                   （前日モデルを played/ へ退避 → 次の最古モデルへ切替）
fresh/ が空になったら → played/ の最大ステップモデルを繰り返し再生
```

> 1 回の学習で出るモデルが 1 本の場合、`fresh/` は 1 日で消費される。日ごとに切り替えたいなら
> 学習を複数回走らせて `fresh/` に run を溜めるか、`played/` フォールバック再生に任せる。

### ツール

| スクリプト | タイミング | 役割 |
|---|---|---|
| `tools\advance_day.ps1` | 平日 14:00（自動） | `fresh/` 最古モデルで ai_server を（再）起動。前回モデルを `played/` へ退避 |
| `tools\local_loop.ps1` | ローカル観察時 | `fresh/` を昇順に1巡再生して終了（`played/` 移動なし） |
| `tools\demo_checkpoints.ps1` | 開発時の手動確認 | 一覧から選んで再生 |

### セットアップ手順

```powershell
# 1. 学習を実行（通常は Stage 4 卒業プリセット 1 本が fresh/ に生成される）
#    total-timesteps は安全上限（タイムアウト）。Stage 4 卒業で自動終了。
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4 --total-timesteps 2000000

# 2. day 1 から配信開始（fresh/ の最古モデルで起動）
tools\advance_day.ps1

# 翌日: day 2 へ切り替え（前日モデルを played/ へ退避し次のモデルで起動）
tools\advance_day.ps1
```

### Windows タスクスケジューラ設定

```powershell
$root = "C:\Users\iii03\block-stacker"

# 平日 14:00: advance_day（非ブロッキング）
$triggerAdv = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "14:00"
$actionAdv  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File tools\advance_day.ps1 -DurationSeconds 86400" `
    -WorkingDirectory $root
Register-ScheduledTask -TaskName "BlockStacker-AdvanceDay" `
    -Trigger $triggerAdv -Action $actionAdv -Force
```

### 手動操作・デバッグ

```powershell
# 何のモデルを使うか確認（ai_server は起動しない）
tools\advance_day.ps1 -DryRun

# 1 日あたり 2 時間だけ配信（7200 秒で自動終了）
tools\advance_day.ps1 -DurationSeconds 7200

# ai_server を直接起動するときも --duration が使える
.venv\Scripts\python.exe -m block_stacker.serving.ai_server --duration 300

# advance_state.json を確認（今日のモデルと PID）
Get-Content output\training\advance_state.json
```

### ディレクトリ構造

```
output/
  training/
    fresh/                 ← 学習直後の新しいモデル（advance_day が消費して played/ へ）
      sac_20260713-100000_198000_steps.zip   ← Stage 4 卒業プリセット（通常はこの 1 本）
    played/                ← advance_day.ps1 が再生後に移動したモデル（run ごとに共存・衝突なし）
      sac_20260706-091500_342000_steps.zip   ← 先週の run（別 run_ts で衝突しない）
      ...
    advance_state.json     ← {"model": "...", "from_fresh": true, "started_at": "...", ...}
    checkpoints/           ← 旧ディレクトリ（残っていても自動的に使わない）
    replay_buffer.pkl
    resume_state.json
```

---

## 関連

- 学習スクリプト: [`src/block_stacker/training/train.py`](../src/block_stacker/training/train.py)
- 推論サーバ: [`src/block_stacker/serving/ai_server.py`](../src/block_stacker/serving/ai_server.py)
- **ライブ配信モード**: [`docs/live_mode.md`](live_mode.md)（`live_server.py` の起動・n_envs 設定・スナップショット引き継ぎ手順）
- ヘルパー: [`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1)、[`tools/local_loop.ps1`](../tools/local_loop.ps1)、[`tools/advance_day.ps1`](../tools/advance_day.ps1)
- **ログ解読マニュアル**: [`docs/log_reading.md`](log_reading.md)（学習/推論ログの読み方）
- 設計書: [`docs/block_stacker_design.md`](block_stacker_design.md)（3 層記憶アーキテクチャ §3）
- デプロイ手順書: [`docs/aws_deployment.md`](aws_deployment.md)（§付録 E §1 に記憶仕様の詳細）
