# ローカル試運転手順書（成長観察デモ）

ローカルで SAC を学習させながら、**timestep ごとの checkpoint** をデモ再生して、AI の
成長過程を観察するためのガイド。

> AWS にデプロイする本番は別途 [`docs/aws_deployment.md`](aws_deployment.md) を参照。
> こちらは試運転・動作確認・成長観察用のローカルワークフロー。

## 概要

```
┌──────────────────────────────────────────────────────────┐
│ 1. 学習を回す                                            │
│    python -m block_stacker.training.train --total-timesteps 4000  │
│    → output/mvp2/fresh/ に sac_20260627-143022_800_steps.zip...  │
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
│ 3. checkpoint を順番にデモ                              │
│    tools\demo_checkpoints.ps1                            │
│    → ai_server を一つずつ起動、各 60 秒視聴            │
│    → step 5,000 → 25,000 → 100,000 と切替えていく       │
└──────────────────────────────────────────────────────────┘
                            │
                            ▼
            AI が「だんだん上手くなる」のを目視確認
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

**既定でオートカリキュラム（Stage 1→5 を自動進行）**。`--total-timesteps` は「**全ステージ合計の上限**」で
（総手数は必ずこの値以下＝時間が読める。早く卒業した残りは次ステージへ回る）、
**①散布0（全ブロックを縦タワーに積み切る）で即卒業、または ②目標高さ到達の成功率が 0.6（直近30エピソード）**
で卒業して次ステージへ。卒業できなければ予算消化で中断。目標高さ＝在庫満積み高さ×`ratio`（既定 0.6）。

> **最終ステージ（Stage 5）卒業後の挙動**: Stage 5 が卒業条件を満たしても `total_timesteps` に
> 到達するまで**最終ステージ環境で学習を継続**する（`reset_num_timesteps=False` で通算ステップ数を
> 引き継ぐ）。これにより checkpoint が `total_timesteps` まで確実に埋まり、週次デモで使える
> ステップ数の幅が最大化される。

```powershell
# 全ステージ（既定。フラグ不要でカリキュラム ON）
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 4000

# まず Stage 1→2 だけ試す
.venv\Scripts\python.exe -m block_stacker.training.train --max-stage 2 --n-envs 6 --total-timesteps 4000

# 単一ステージだけ素早く確認したいとき（Stage 1 のみ）
.venv\Scripts\python.exe -m block_stacker.training.train --no-curriculum --n-envs 6 --total-timesteps 4000
```
> 新人 AI は短い予算だと卒業条件に届かず「卒業せず中断」になる。進行そのものを素早く見たいだけなら
> `configs/training.yaml` の `graduation.threshold` を下げる／`ratio` を下げる（目標を低く）。
> これらは **環境変数 `BS_GRADUATION_THRESHOLD` / `BS_GRADUATION_RATIO` / `BS_GRADUATION_WINDOW`**
> でも上書きできる（env var > training.yaml > 既定）。本番（AWS の learner）も既定でカリキュラム ON。

学習中：
- `output/mvp2/fresh/sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip` が **`total_timesteps` の 20/40/60/80/100% 地点**で保存される（5本固定）。
  先頭の日時（`run_ts`）は同一 run の 5 本で共通。ファイル名のステップ数は全ステージ通算の連続値。`configs/training.yaml` の `sac.checkpoint_splits`（既定 5）で分割数を変更可。
  最後の checkpoint（100% 地点）が最終モデル相当（`sac_final.zip` は廃止）。
- `output/mvp2/tb/` に TensorBoard ログが書かれる
- `output/mvp2/replay_buffer.pkl`（長期記憶）と `output/mvp2/resume_state.json` が**毎回**保存される（次回 `--resume` で利用）
- **前回の学習の `fresh/` が残っている場合**: 学習開始時に自動で `played/` へ退避してから新しい checkpoint を `fresh/` に生成する

#### 前回の学習を引き継ぐ（--resume）

前回の学習が終わった後、続きから学習を再開できる。**勘（NN重み）** と **長期記憶（replay buffer）**
を引き継ぎ、長期記憶には「前回終了からの経過日数×5000 step 分の時間減衰」を自動適用する。

```powershell
# 初回学習を済ませてから続きを学習（find_latest_checkpoint で最新 run の最大ステップ checkpoint を自動選択）
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 4000 --resume
```

- **経過日数は自動算出**（`resume_state.json` の `timestamp` から現在時刻との差を計算）。
- 減衰の強さを調整したいときは `configs/training.yaml` の `resume.steps_per_day`（既定 5000）を変更。
- テスト用に手動で経過日数を指定するには `resume.elapsed_days` を設定するか、
  `resume_state.json` の `timestamp` を書き換える。
- カリキュラム進捗も引き継がれ、`resume_state.json` の `next_stage_id` から再開する。

別ターミナルで TensorBoard を起動すると学習曲線がリアルタイムで見られる：

```powershell
.venv\Scripts\python.exe -m tensorboard.main --logdir output\mvp2\tb
# ブラウザで http://localhost:6006 を開く
```

### Step 2: Godot クライアントを起動

学習中でも学習後でも OK。

```powershell
& "D:\Godot_v4.4.1-stable_mono_win64\Godot_v4.4.1-stable_mono_win64.exe" `
    --path C:\Users\iii03\block-stacker\client res://scenes/main.tscn
```

サーバ未起動なので「**サーバとの通信を試行中**...」と表示される。

### Step 3: チェックポイントを比較

[`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1) を使う。

> **デモは常に最終ステージ（全形状）でモデルを動かす**（`ai_server` 既定）。`--model` 無指定なら
> `fresh/` または `played/` の最大ステップ checkpoint を自動選択。Stage 1 しか学習していない
> checkpoint を最終ステージの世界（円柱あり）で再生すると当然うまく積めない点に注意。特定ステージの
> 世界で見たいなら `ai_server --stage N` を使う。
>
> **散布ブロックゼロになったら自動で仕切り直す**: 全ブロックを積み切る（または物理破綻で拾える
> 散布ブロックが無くなる）と、`ai_server` は**全ブロックを再ランダム配置**してラウンドを再開する
> （body_id は保持するので配信は途切れない）。MVP では演出なし（将来リセット演出を入れる余地あり）。

#### 対話モード（一つ選んで再生）

```powershell
tools\demo_checkpoints.ps1
```

出力例：

```
発見された checkpoint (5 件):
  0: 20260627-143022 / 800 steps   (sac_20260627-143022_800_steps.zip)
  1: 20260627-143022 / 1600 steps  (sac_20260627-143022_1600_steps.zip)
  2: 20260627-143022 / 2400 steps  (sac_20260627-143022_2400_steps.zip)
  3: 20260627-143022 / 3200 steps  (sac_20260627-143022_3200_steps.zip)
  4: 20260627-143022 / 4000 steps  (sac_20260627-143022_4000_steps.zip)

番号を入力 (例: 5)、'all' で全部、'q' で終了
> 0
=== 20260627-143022 / 800 steps (sac_20260627-143022_800_steps.zip) ===
  ai_server PID 12345、 60 秒間再生...
```

→ Godot 画面で AI の動きを観察できる。

#### Auto モード（全部順番に）

```powershell
tools\demo_checkpoints.ps1 -Mode auto -Seconds 30
```

20 個の checkpoint を各 30 秒ずつ自動で順番再生。**約 10 分かけて AI の成長を一気に見られる**。

#### ローカル成長1巡再生（local_loop.ps1）

`fresh/` の checkpoint を古い→新しい順に1巡再生し、最後のモデルが終わったら**自動終了**する（ループなし）。

```powershell
# fresh/ を 60 秒ずつ1巡して終了
tools\local_loop.ps1

# 30 秒ごとに切り替えて1巡
tools\local_loop.ps1 -SwitchSeconds 30

# played/ を指定（先の学習分を観察したいとき）
tools\local_loop.ps1 -Dir output\mvp2\played
```

`played/` を直接選んで再生したいときは `demo_checkpoints.ps1 -CheckpointsDir output\mvp2\played` も使える。

### Step 4: 観察する

| timestep | 期待される挙動 | コンセプトメタファー |
|---------|------------|----------------|
| 5,000 | 完全ランダム、ブロックを宙に投げて落下 | 「赤ちゃんが触る」 |
| 25,000 | ブロックを掴むようになる、置き場所が雑 | 「2 歳児: 手は動くがズレる」 |
| 50,000 | 1〜2 段は積める、たまに崩す | 「3 歳児: 上に乗せられる」 |
| 100,000 | 安定して 2〜3 段、迷い動作減少 | 「4 歳児: コツを掴み始める」 |
| 250,000 | 3〜5 段、形状を選んで積む | 「子供: 楽しく上手に積める」 |
| 500,000+ | 円柱を最後に乗せる、安定タワー | 「コツを掴んだ子供」 |

具体的な動作目安：

| 観察ポイント | 早期 | 中期 | 後期 |
|----------|------|------|------|
| ブロックを掴むか | 失敗多 | ほぼ成功 | 成功 |
| タワーの近くに置くか | 関係ない場所 | 近づく | 真上に置く |
| タワー崩落 | 頻繁 | たまに | ほぼなし |
| 形状選択 | ランダム | 安定形状を選ぶ | 平面ブロック優先 |

## ヘルパースクリプトの引数

### `tools\demo_checkpoints.ps1`（手動 checkpoint 比較用）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-CheckpointsDir` | `output\mvp2\fresh` | checkpoint ディレクトリ（fresh/ または played/ を指定） |
| `-Seconds` | 60 | 各 checkpoint の再生時間（秒）|
| `-Mode` | `interactive` | `interactive` (一つ選ぶ) または `auto` (全部順番) |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-Godot` | `D:\Godot_...\Godot_...exe` | Godot 実行パス |
| `-LaunchGodot` | (未指定なら手動) | このフラグで Godot を自動起動 |

### `tools\local_loop.ps1`（ローカル成長1巡再生）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-Dir` | `output\mvp2\fresh` | checkpoint ディレクトリ |
| `-SwitchSeconds` | `60` | 1 モデルあたりの再生秒数 |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-AiHost` | `127.0.0.1` | ai_server の listen ホスト |
| `-AiPort` | `8765` | ai_server の listen ポート |

### `tools\advance_day.ps1`（日次 ai_server 切り替え）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-FreshDir` | `output\mvp2\fresh` | 新しい checkpoint のディレクトリ |
| `-PlayedDir` | `output\mvp2\played` | 再生済み checkpoint の退避先 |
| `-StateFile` | `output\mvp2\advance_state.json` | 前回状態の記録ファイル |
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
| 各 checkpoint で全く違う動き | 学習途中の SAC は不安定な時期がある（特に 5k〜30k）。後期 checkpoint に進むと安定する |

## 学習時間の見積もり

i7-10750H (6 物理コア) 想定:

| total_timesteps | 学習時間 | checkpoint 数 | デモ全部見るのに |
|---------|---------|------------|----------|
| **4,000** (週次標準) | **約 1 分** | **5 個** | **各 60s で 5 分** |
| 500,000 | 約 2 時間 | 5 個 | 各 60s で 5 分 |
| 1,000,000 | 約 4 時間 | 5 個 | auto モード 30s で 2.5 分 |

→ 週次配信は `4,000` が標準（checkpoint_splits=5 で 800 刻み 5 本）。本格学習は 500k+ 推奨。

## クラウド学習との関係

このローカル workflow は「**試運転**」用。本番学習は AWS の learner EC2 が
隔週土曜 14-22 に走り、モデルを S3 に保存する。

ローカルで作ったモデルを AWS のデモ側で再生したい場合は S3 にアップ：

```powershell
$ACCOUNT = aws sts get-caller-identity --query Account --output text
# fresh/ の最大ステップ checkpoint を最新モデルとして S3 にアップ
$model = & .venv\Scripts\python.exe -c "from block_stacker.training.checkpoint import find_latest_checkpoint; from pathlib import Path; p = find_latest_checkpoint(Path('output/mvp2')); print(p)"
aws s3 cp $model s3://bs-app-$ACCOUNT/models/latest.pt
```

→ 次のクラウドデモ起動時にこのモデルが自動的に読み込まれる。

---

## 日次配信モード（fresh/played 方式）

学習で生成した checkpoint を `fresh/` に蓄積し、**`advance_day.ps1` を毎日呼ぶだけで
古い→新しい順に自動ステップアップ**して配信する。

```
学習後           → fresh/ に sac_20260627-143022_800_steps.zip ... sac_20260627-143022_4000_steps.zip が 5 本生成
advance_day.ps1  → fresh/ の最古モデルで ai_server を起動
                   （前日モデルを played/ へ退避 → 次の最古モデルへ切替）
fresh/ が空になったら → played/ の最大ステップモデルを繰り返し再生
```

### ツール

| スクリプト | タイミング | 役割 |
|---|---|---|
| `tools\advance_day.ps1` | 平日 14:00（自動） | `fresh/` 最古モデルで ai_server を（再）起動。前回モデルを `played/` へ退避 |
| `tools\local_loop.ps1` | ローカル観察時 | `fresh/` を昇順に1巡再生して終了（`played/` 移動なし） |
| `tools\demo_checkpoints.ps1` | 開発時の手動確認 | 一つ選んで再生または全自動 |

### セットアップ手順

```powershell
# 1. 学習を実行（total_timesteps を 5 等分した地点で fresh/ に checkpoint が生成される）
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 6 --total-timesteps 4000

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
Get-Content output\mvp2\advance_state.json
```

### ディレクトリ構造

```
output/
  mvp2/
    fresh/                 ← 学習直後の新しい checkpoint（advance_day が消費して played/ へ）
      sac_20260627-143022_800_steps.zip
      sac_20260627-143022_1600_steps.zip
      sac_20260627-143022_2400_steps.zip
      sac_20260627-143022_3200_steps.zip
      sac_20260627-143022_4000_steps.zip
    played/                ← advance_day.ps1 が再生後に移動した checkpoint（run ごとに共存・衝突なし）
      sac_20260620-091500_4000_steps.zip   ← 先週の run（別 run_ts で衝突しない）
      sac_20260627-143022_800_steps.zip    ← 今週の 1 日目終わりに移動
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
- ヘルパー: [`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1)、[`tools/local_loop.ps1`](../tools/local_loop.ps1)、[`tools/advance_day.ps1`](../tools/advance_day.ps1)
- **ログ解読マニュアル**: [`docs/log_reading.md`](log_reading.md)（学習/推論ログの読み方）
- 設計書: [`docs/block_stacker_design.md`](block_stacker_design.md)（3 層記憶アーキテクチャ §3）
- デプロイ手順書: [`docs/aws_deployment.md`](aws_deployment.md)（§付録 E §1 に記憶仕様の詳細）
