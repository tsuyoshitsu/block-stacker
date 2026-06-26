# ローカル試運転手順書（成長観察デモ）

ローカルで SAC を学習させながら、**timestep ごとの checkpoint** をデモ再生して、AI の
成長過程を観察するためのガイド。

> AWS にデプロイする本番は別途 [`docs/aws_deployment.md`](aws_deployment.md) を参照。
> こちらは試運転・動作確認・成長観察用のローカルワークフロー。

## 概要

```
┌──────────────────────────────────────────────────────────┐
│ 1. 学習を回す                                            │
│    python -m block_stacker.mvp2.train --total-timesteps 4000  │
│    → output/mvp2/checkpoints/ に sac_5000_steps.zip...  │
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
.venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 4000

# まず Stage 1→2 だけ試す
.venv\Scripts\python.exe -m block_stacker.mvp2.train --max-stage 2 --n-envs 6 --total-timesteps 4000

# 単一ステージだけ素早く確認したいとき（Stage 1 のみ）
.venv\Scripts\python.exe -m block_stacker.mvp2.train --no-curriculum --n-envs 6 --total-timesteps 4000
```
> 新人 AI は短い予算だと卒業条件に届かず「卒業せず中断」になる。進行そのものを素早く見たいだけなら
> `configs/training.yaml` の `graduation.threshold` を下げる／`ratio` を下げる（目標を低く）。
> これらは **環境変数 `BS_GRADUATION_THRESHOLD` / `BS_GRADUATION_RATIO` / `BS_GRADUATION_WINDOW`**
> でも上書きできる（env var > training.yaml > 既定）。本番（AWS の learner）も既定でカリキュラム ON。

学習中：
- `output/mvp2/checkpoints/sac_<steps>_steps.zip` が **`total_timesteps` の 20/40/60/80/100% 地点**で保存される（5本固定）。
  ファイル名のステップ数は全ステージ通算の連続値。`configs/training.yaml` の `sac.checkpoint_splits`（既定 5）で分割数を変更可。
  → 週次配信の `step_01..05.zip` と 1 対 1 で対応する設計。
- `output/mvp2/tb/` に TensorBoard ログが書かれる
- `output/mvp2/sac_final.zip`（最終モデルのみ）が保存される。ステージごとの最終モデルは保存せず、checkpoints/ で補完
- `output/mvp2/replay_buffer.pkl`（長期記憶）と `output/mvp2/resume_state.json` が**毎回**保存される（次回 `--resume` で利用）

#### 前回の学習を引き継ぐ（--resume）

前回の学習が終わった後、続きから学習を再開できる。**勘（NN重み）** と **長期記憶（replay buffer）**
を引き継ぎ、長期記憶には「前回終了からの経過日数×5000 step 分の時間減衰」を自動適用する。

```powershell
# 初回学習（100,000 ステップ）を済ませてから続きを学習
.venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 4000 --resume
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
> `sac_final.zip`→`sac_stage1_final.zip` の順で自動選択。Stage 1 しか学習していない checkpoint を
> 最終ステージの世界（円柱あり）で再生すると当然うまく積めない点に注意。特定ステージの世界で見たい
> なら `ai_server --stage N` を使う。
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
発見された checkpoint (20 件):
  0: 5000 steps       (sac_5000_steps.zip)
  1: 10000 steps      (sac_10000_steps.zip)
  2: 15000 steps      (sac_15000_steps.zip)
  ...
 19: 100000 steps     (sac_100000_steps.zip)

番号を入力 (例: 5)、'all' で全部、'q' で終了
> 0
=== 5000 steps (sac_5000_steps.zip) ===
  ai_server PID 12345、 60 秒間再生...
```

→ Godot 画面で AI の動きを観察できる。

#### Auto モード（全部順番に）

```powershell
tools\demo_checkpoints.ps1 -Mode auto -Seconds 30
```

20 個の checkpoint を各 30 秒ずつ自動で順番再生。**約 10 分かけて AI の成長を一気に見られる**。

#### 週次モデルを再生（curate_week.ps1 との連携）

`-CheckpointsDir` に `output\weeks\<YYYY-WNN>` を渡すと、`step_NN.zip`（週次モデル）を
`day N` 形式で再生できる。`checkpoints/` の `sac_<steps>_steps.zip` との後方互換はそのまま保たれる。

```powershell
# 週次モデルを対話モードで選んで再生
tools\demo_checkpoints.ps1 -CheckpointsDir output\weeks\2026-W26

# まとめて 30 秒ずつ再生（mon → fri の成長を一気に見る）
tools\demo_checkpoints.ps1 -CheckpointsDir output\weeks\2026-W26 -Mode auto -Seconds 30
```

出力例（週次モデル）：

```
発見された checkpoint (5 件):
  0: day 01           (step_01.zip)
  1: day 02           (step_02.zip)
  2: day 03           (step_03.zip)
  3: day 04           (step_04.zip)
  4: day 05           (step_05.zip)

番号を入力 (例: 5)、'all' で全部、'q' で終了
> 0
=== day 01 (step_01.zip) ===
  ai_server PID 12345、 30 秒間再生...
```

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
| `-CheckpointsDir` | `output\mvp2\checkpoints` | checkpoint 保存先 |
| `-Seconds` | 60 | 各 checkpoint の再生時間（秒）|
| `-Mode` | `interactive` | `interactive` (一つ選ぶ) または `auto` (全部順番) |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-Godot` | `D:\Godot_...\Godot_...exe` | Godot 実行パス |
| `-LaunchGodot` | (未指定なら手動) | このフラグで Godot を自動起動 |

### `tools\curate_week.ps1`（週次 checkpoint 選出）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-CheckpointsDir` | `output\mvp2\checkpoints` | checkpoint 入力先 |
| `-WeeksDir` | `output\weeks` | weeks 出力先 |
| `-FinalModelPath` | `output\mvp2\sac_final.zip` | 5本未満時のパディング用モデル |
| `-WeekOverride` | `""` (今週の ISO 週番号) | 週番号を手動指定（テスト用） |
| `-MaxSteps` | `0` (上限なし) | この値以下の checkpoint だけを選出対象にする |
| `-Force` | (なし) | 既存週ディレクトリを上書き |

### `tools\advance_day.ps1`（日次 ai_server 切り替え）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-WeeksDir` | `output\weeks` | weeks ディレクトリ |
| `-FinalModelPath` | `output\mvp2\sac_final.zip` | weeks 未設定時のフォールバック |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-AiHost` | `127.0.0.1` | ai_server の listen ホスト |
| `-AiPort` | `8765` | ai_server の listen ポート |
| `-DurationSeconds` | `0` (無制限) | 0 以外なら ai_server に `--duration` を渡す |
| `-DryRun` | (なし) | 表示のみ、ai_server は起動しない |
| `-NoAdvance` | (なし) | 起動するが `current_day` を進めない |

### `ai_server`（推論・配信サーバ）の主要オプション

| オプション | デフォルト | 説明 |
|----------|---------|------|
| `--model` | 自動選択 | モデルパス（未指定: `sac_final.zip`→`sac_stage1_final.zip`） |
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
| `checkpoints ディレクトリが見つかりません` | 先に `mvp2.train` を実行して学習を回す |
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
aws s3 cp output\mvp2\sac_final.zip s3://bs-app-$ACCOUNT/models/latest.pt
```

→ 次のクラウドデモ起動時にこのモデルが自動的に読み込まれる。

---

## 週次配信モード（月〜金で成長を段階的に見せる）

学習済み checkpoint を週単位でキュレーションし、**月〜金の 5 日間で段階的にモデルを
ステップアップ**して配信する運用モード。

> **checkpoint と step_01..05 の対応**: `train.py` は学習を `total_timesteps` の 20/40/60/80/100%
> の地点でちょうど 5 本の checkpoint を生成する（`configs/training.yaml` の `checkpoint_splits: 5`）。
> `curate_week.ps1` がこれを選出して `step_01..05.zip` に並べるので、
> **checkpoint 5 本 ↔ step_01..05 が 1 対 1 で対応**する。

```
日曜 学習後 → curate_week.ps1 → weeks/<YYYY-WNN>/ に step_01〜05.zip を配置
月曜 14:00  → advance_day.ps1 → step_01.zip で ai_server 起動 (day 1)
火曜 14:00  → advance_day.ps1 → step_02.zip で ai_server 起動 (day 2)
...
金曜 14:00  → advance_day.ps1 → step_05.zip で ai_server 起動 (day 5)
土・日      → step_05.zip 固定表示（次の日曜に新モデルセットが来るまで）
```

### ツール

| スクリプト | タイミング | 役割 |
|---|---|---|
| `tools\curate_week.ps1` | 学習直後（日曜） | checkpoint 群から等間隔 5 本選出 → `weeks/<YYYY-WNN>/` 生成 |
| `tools\advance_day.ps1` | 平日 14:00（自動） | 今日の step モデルで ai_server を（再）起動、current_day を +1 |
| `tools\demo_checkpoints.ps1` | 開発時の手動確認 | 変更なし・開発用として温存 |

### 初回セットアップ手順

```powershell
# 1. 学習を実行（total_timesteps を 5 等分した地点で checkpoint が生成される）
.venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 4000

# 2. checkpoint をキュレーション（今週の weeks/<YYYY-WNN>/ を生成）
tools\curate_week.ps1

# 3. day 1 から配信開始
tools\advance_day.ps1
```

### Windows タスクスケジューラ設定

PowerShell（管理者）で実行する（パスはインストール先に合わせて変更）：

```powershell
$root = "C:\Users\iii03\block-stacker"

# 平日 14:00: advance_day
$triggerAdv = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "14:00"
$actionAdv  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File tools\advance_day.ps1" `
    -WorkingDirectory $root
Register-ScheduledTask -TaskName "BlockStacker-AdvanceDay" `
    -Trigger $triggerAdv -Action $actionAdv -Force

# 日曜 15:00: 学習 + キュレーション（学習に数時間かかるため開始を早めに設定）
# 学習とキュレーションは下記コマンドを 1 スクリプトにまとめて登録するか、
# 学習完了後に手動で curate_week.ps1 を実行する。
$triggerCur = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "08:00"
$actionCur  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -Command `".venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 4000; tools\curate_week.ps1 -Force`"" `
    -WorkingDirectory $root
Register-ScheduledTask -TaskName "BlockStacker-WeeklyCurate" `
    -Trigger $triggerCur -Action $actionCur -Force
```

### オプション設定

#### 最大ステップ数の上限を指定（curate_week.ps1 `-MaxSteps`）

`-MaxSteps` を指定すると、そのステップ数**以下**の checkpoint だけを対象に等間隔選出する。
指定値ちょうどのファイルが無くても、それ以下で最大のものが step_05 に入る（自動追従）。
未指定（既定 0）なら従来どおり全 checkpoint の最大値が step_05 に入る。

```powershell
# 5万ステップ以下の checkpoint から 5 本選ぶ（週の「成長」幅を抑えたいとき）
tools\curate_week.ps1 -MaxSteps 50000

# 合わせて週番号も指定する場合
tools\curate_week.ps1 -WeekOverride 2026-W27 -MaxSteps 50000 -Force
```

#### デモの自動終了時間を指定（advance_day.ps1 `-DurationSeconds` / ai_server `--duration`）

`-DurationSeconds` を指定すると、ai_server がその秒数後に自動終了する（`--duration` として渡る）。
未指定（既定 0）なら従来どおり常駐し、次回 advance_day.ps1 が Kill するまで動き続ける。

```powershell
# 1 日あたり 2 時間だけ配信（7200 秒で自動終了）
tools\advance_day.ps1 -DurationSeconds 7200

# ai_server を直接起動するときも同様のオプションが使える
.venv\Scripts\python.exe -m block_stacker.mvp3.ai_server --model output\mvp2\sac_final.zip --duration 300
```

### 手動操作・デバッグ

```powershell
# 何のモデルを使うか確認（ai_server は起動しない）
tools\advance_day.ps1 -DryRun

# duration 付き DryRun（渡されるオプションを確認）
tools\advance_day.ps1 -DryRun -DurationSeconds 7200

# 週番号を手動指定してキュレーション（テスト用など）
tools\curate_week.ps1 -WeekOverride 2026-W26 -Force

# MaxSteps フィルタ確認（DryRun 相当: 実際にコピーされるので -Force も必要）
tools\curate_week.ps1 -WeekOverride 2026-W26 -MaxSteps 50000 -Force

# state.json の current_day を手動で見る/書き換える
Get-Content output\weeks\(Get-Content output\weeks\active_week.txt)\state.json
```

### ディレクトリ構造

```
output/
  weeks/
    active_week.txt        ← "2026-W26"（アクティブな週）
    2026-W26/
      manifest.json        ← 選出した step の一覧
      state.json           ← {"current_day": 3, "last_advanced": "2026-06-18"}
      step_01.zip          ← day 1 (月) のモデル
      step_02.zip          ← day 2 (火)
      step_03.zip          ← day 3 (水)
      step_04.zip          ← day 4 (木)
      step_05.zip          ← day 5 (金) / 土・日も固定でこれを使用
  mvp2/
    sac_final.zip          ← 最新最終モデル（weeks/ 未設定時のフォールバック）
    checkpoints/           ← 学習中の生 checkpoint（一時的）
```

---

## 関連

- 学習スクリプト: [`src/block_stacker/mvp2/train.py`](../src/block_stacker/mvp2/train.py)
- 推論サーバ: [`src/block_stacker/mvp3/ai_server.py`](../src/block_stacker/mvp3/ai_server.py)
- ヘルパー: [`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1)、[`tools/curate_week.ps1`](../tools/curate_week.ps1)、[`tools/advance_day.ps1`](../tools/advance_day.ps1)
- **ログ解読マニュアル**: [`docs/log_reading.md`](log_reading.md)（学習/推論ログの読み方）
- 設計書: [`docs/block_stacker_design.md`](block_stacker_design.md)（3 層記憶アーキテクチャ §3）
- デプロイ手順書: [`docs/aws_deployment.md`](aws_deployment.md)（§付録 E §1 に記憶仕様の詳細）
