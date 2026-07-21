# ローカル試運転手順書

ローカルで SAC を学習させ、生成したモデルをデモ再生して動作を確認するためのガイド。

> AWS にデプロイする本番は別途 [`docs/aws_deployment.md`](aws_deployment.md) を参照。
> こちらは試運転・動作確認用のローカルワークフロー。

## 概要

```
┌──────────────────────────────────────────────────────────┐
│ 1. 学習を回す                                            │
│    python -m block_stacker.training.train --n-envs 4                 │
│    → output/training/fresh/ にモデルが 1 本できる            │
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
│ 3. モデルを再生（ai_server）                             │
│    または live_server で「学習しながら」配信              │
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

**既定でオートカリキュラム（Stage 1→4 を自動進行）**。進行は**固定ステップ制**で、各ステージは
決められたステップ数だけ走り、**成績によらず**次のステージへ進む（卒業判定は廃止）。

> **なぜ卒業判定が無いのか**: かつては「散布0（全ブロックを積み切る）で即卒業」という条件があったが、
> これは横に広い低い構造でも成立してしまい（高さ 0.100m でも成立することを実測確認）、
> 成功率 0 のまま Stage 1→4 が数千ステップで飛ぶ原因になっていた。経緯は
> [`docs/design_change_record.md`](design_change_record.md) §1.2.1。

ステージ予算は `configs/training.yaml` の `curriculum.stages[].steps`。既定値：

| Stage | 内容 | steps | 配分の根拠 |
|---|---|---|---|
| 1 | cube 8個 | **60,000** | ゼロから基礎を獲得する最大の山。**以降の全ステージがこの方策を継承する** |
| 2 | cube 15個 | 35,000 | 同じ cube のまま個数と目標高さが増えるだけ。転移が最も効く |
| 3 | +cuboid | 25,000 | 実測で予算内に success_rate 0.43 到達。余裕があるので Stage 4 へ回した |
| 4 | +三角柱 | **60,000** | **斜面**。実測で 0.43 → 0.00 に退行し、回復途中で予算切れになった |
| 5 | +円柱 | **70,000** | **曲面**（転がる）。既存方策が通用しない新規スキル |
| | **合計** | **250,000** | 既定範囲（Stage 1-4）は **180,000** |

重いのは Stage 1（ゼロから）と Stage 4・5（斜面・曲面という**質的に新しい難しさ**）です。
難易度は形状が支配的で、目標高さの大小では測れません（Stage 3→4 は目標が 0.408m→0.420m と
ほぼ同じなのに success_rate が 0.43→0.00 に落ちた）。

> **総量の根拠**: このマシンの実測スループットは**約 2 steps/秒**（`time/fps` 中央値 2.0）。
> 既定範囲の 180,000 steps で**約 25 時間**、全 5 ステージの 250,000 steps で**約 35 時間**。
> 学習シグナル自体は 1〜2 万ステップで現れる。増やす場合は所要時間が比例して伸びる点に注意。

```powershell
# ---- プリセット生成（既定: Stage 1→4 を各予算どおり走る）----
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4

# Stage 5（全形状、円柱含む）まで走らせる
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4 --target-stage 5

# 全ステージを一括で同じステップ数にする（一括指定）
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4 --stage-steps 100000

# ステージごとに個別指定（実行するステージ数と要素数を合わせる。ここでは Stage 1-4 の4要素）
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4 --stage-steps 50000,30000,35000,40000

# まず Stage 1→2 だけ試す
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4 --target-stage 2

# 単一ステージだけ素早く確認したいとき（Stage 1 のみ）
.venv\Scripts\python.exe -m block_stacker.training.train --no-curriculum --n-envs 4 --stage-steps 20000

# 動作確認だけ（数秒で終わる超短縮版）
.venv\Scripts\python.exe -m block_stacker.training.train --no-curriculum --n-envs 2 --stage-steps 500
```

> `--stage-steps` はカンマ区切りの要素数が**実行するステージ数と一致しないとエラー**になる
> （黙って切り詰めると学習量が意図とズレるため）。単一値なら全ステージ一括。
> `--total-timesteps` は全体の安全上限で、無指定ならステージ予算の合計がそのまま使われる。
> `BS_GRADUATION_RATIO`（目標高さ係数）と `BS_GRADUATION_WINDOW`（指標の移動平均幅）は引き続き有効。
> `BS_GRADUATION_THRESHOLD` は卒業判定の撤去に伴い**未使用**。

学習中：
- **`fresh/` に残るのは全ステージ走破後のプリセット 1 本だけ**（`sac_final.zip` は廃止）。
  定期 checkpoint は保存しない。途中経過を残したいときは `--stage-steps` を刻んで
  複数回に分けて走らせる（run ごとに 1 本ずつ溜まる）。
- `output/training/tb/` に TensorBoard ログが書かれる
- `output/training/replay_buffer.pkl`（長期記憶）と `output/training/resume_state.json` が**毎回**保存される
  （live_server のスナップショット引き継ぎで利用。**train 側の `--resume` は廃止済み**＝学習の再開は不可）
- **前回の学習の `fresh/` が残っている場合**: 学習開始時に自動で `played/` へ退避してから新しいモデルを `fresh/` に生成する

別ターミナルで TensorBoard を起動すると学習曲線がリアルタイムで見られる：

```powershell
.venv\Scripts\python.exe -m tensorboard.main --logdir output\training\tb
# ブラウザで http://localhost:6006 を開く
```

### Step 2: Godot クライアントを起動

```powershell
& "D:\Godot_v4.4.1-stable_mono_win64\Godot_v4.4.1-stable_mono_win64.exe" `
    --path C:\Users\iii03\block-stacker\client res://scenes/main.tscn
```

この時点ではサーバ未起動なので「**サーバとの通信を試行中**...」と表示される。Step 3 で
サーバを起動すると 2 秒以内に自動接続する。

> **`training.train` の実行中はクライアントから見られない。**
> `training.train` は WebSocket サーバを持たない（物理シムはヘッドレスで並列実行される）。
> さらに学習中はモデルが一切書き出されない（保存は全ステージ走破後の 1 本のみ）ので、
> 別途 `ai_server` を並走させても**古いモデルを映し続けるだけ**になる。
> **学習しながら見たい場合は次項の `live_server` を使うこと。**

### Step 3: モデルを再生

学習済みモデルを `ai_server` で動かす。`--model` 無指定なら `fresh/` / `played/` から
最大ステップのモデルが自動選択される。

```powershell
.venv\Scripts\python.exe -m block_stacker.serving.ai_server --host 127.0.0.1
```

複数の run のモデルが溜まっている場合は、[`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1)
で一覧から選んで再生できる。

```powershell
tools\demo_checkpoints.ps1
```

出力例（学習を 2 回走らせて run が 2 つある場合）：

```
発見された checkpoint (2 件):
  0: 20260713-100000 / 180000 steps  (sac_20260713-100000_180000_steps.zip)
  1: 20260714-093000 / 180000 steps  (sac_20260714-093000_180000_steps.zip)

番号を入力 (例: 0)、'all' で全部、'q' で終了
> 1
```

> **1 回の学習で出るモデルは 1 本**（全ステージ走破後のプリセット）。したがってこの一覧に
> 複数並ぶのは「**複数回学習を走らせた**」場合であり、1 run 内の途中経過ではない。
> `run_ts`（先頭の日時）が run の識別子で、同じ run のモデルは同じ値を持つ。
>
> **デモは常に最終ステージ（全形状）でモデルを動かす**（`ai_server` 既定）。特定ステージの
> 世界で見たいなら `ai_server --stage N` を使う。
>
> **散布ブロックゼロになったら自動で仕切り直す**: 全ブロックを積み切る（または物理破綻で拾える
> 散布ブロックが無くなる）と、`ai_server` は**全ブロックを再ランダム配置**してラウンドを再開する
> （body_id は保持するので配信は途切れない）。学習側の env も同じ挙動をする。

`played/` に退避済みのモデルを再生したいときは
`demo_checkpoints.ps1 -CheckpointsDir output\training\played` を使う。

→ Godot 画面で AI の動きを観察できる。

### Step 3-b: 学習しながらリアルタイムで見る（live_server）

**学習の進行をクライアントで見たい場合はこちら。**`live_server` は配信と学習を 1 プロセスに
融合したモードで、WebSocket で配信しながらバックグラウンドで SAC を回し続ける。

```powershell
.venv\Scripts\python.exe -m block_stacker.serving.live_server `
    --snapshot-dir output/training `
    --n-envs 6 `
    --host 127.0.0.1 `
    --duration 0
```

- `--snapshot-dir` を指定するだけで**スナップショット（NN 重み＋長期記憶）を自動で引き継ぐ**
  （`--resume` フラグは不要。無視させたい初回のみ `--no-resume`）
- `--duration 0` で無制限。既定は 28800 秒（8 時間）で自動終了する
- Godot の接続先は `ai_server` と同じ `ws://127.0.0.1:8765`

`training.train` との違い：

| | `training.train` | `live_server` |
|---|---|---|
| クライアント接続 | **不可**（WebSocket なし） | 可 |
| カリキュラム | `stages[].steps` に従って Stage を進行 | **常に最終ステージのみ**（Stage 進行なし） |
| 終了条件 | ステージ予算を使い切ったら | `--duration` |

**`live_server` はカリキュラムを進行しない**ので、段階的に学習させたいときは `training.train`、
できあがったモデルを配信しつつじわじわ伸ばしたいときは `live_server`、と使い分ける。
詳細は [`docs/live_mode.md`](live_mode.md)。

> `--n-envs` は 240Hz の物理ループと CPU を取り合う。配信がカクつく場合は下げる
> （12 論理コアなら 4〜6、配信の滑らかさ優先なら 2〜4）。`--n-envs 0` で配信のみ。

### Step 4: 観察する

再生中に見るポイント：

| 観察ポイント | 学習が足りないとき | 学習が進んだとき |
|----------|------|------|
| ブロックを掴むか | 失敗が多い | ほぼ成功 |
| タワーの近くに置くか | 関係ない場所に置く | 真上に置く |
| タワー崩落 | 頻繁 | ほぼなし |
| 形状選択 | ランダム | 安定する形状を優先 |

Stage 1 しか学習していないモデルを最終ステージの世界（円柱あり）で再生すると当然うまく積めない。
`--target-stage 4`（既定）以上の範囲で学習したモデルで確認すること。

## ヘルパースクリプトの引数

### `tools\demo_checkpoints.ps1`（モデルを選んで再生）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-CheckpointsDir` | `output\training\fresh` | モデルのディレクトリ（fresh/ または played/ を指定） |
| `-Seconds` | 60 | 各モデルの再生時間（秒）|
| `-Mode` | `interactive` | `interactive` (一つ選ぶ) または `auto` (見つかった順に全部) |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-Godot` | `D:\Godot_...\Godot_...exe` | Godot 実行パス |
| `-LaunchGodot` | (未指定なら手動) | このフラグで Godot を自動起動 |

### `tools\local_loop.ps1`（`fresh/` を1巡再生して終了）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-Dir` | `output\training\fresh` | モデルのディレクトリ（1 run = 1 本なので、複数あるのは複数回学習した場合） |
| `-SwitchSeconds` | `60` | 1 モデルあたりの再生秒数 |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-AiHost` | `127.0.0.1` | ai_server の listen ホスト |
| `-AiPort` | `8765` | ai_server の listen ポート |

### `tools\advance_day.ps1`（日次 ai_server 切り替え）

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-FreshDir` | `output\training\fresh` | 未再生モデルのディレクトリ |
| `-PlayedDir` | `output\training\played` | 再生済みモデルの退避先 |
| `-StateFile` | `output\training\advance_state.json` | 前回状態の記録ファイル |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-AiHost` | `127.0.0.1` | ai_server の listen ホスト |
| `-AiPort` | `8765` | ai_server の listen ポート |
| `-DurationSeconds` | `0` (無制限) | 0 以外なら ai_server に `--duration` を渡す |
| `-DryRun` | (なし) | 表示のみ、ai_server は起動しない |

### `ai_server`（推論・配信サーバ）の主要オプション

| オプション | デフォルト | 説明 |
|----------|---------|------|
| `--model` | 自動選択 | モデルパス（未指定: `fresh/` / `played/` の最大ステップのモデル） |
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
| `checkpoint ディレクトリが見つかりません` | 先に `training.train` を実行して学習を回す（`fresh/` にモデルが 1 本できる） |
| `ai_server が起動直後に終了` | モデルファイル破損 / 観測形状ミスマッチ。`configs/training.yaml` を変えていないか確認 |
| Godot で AI が動かない | サーバ側ターミナルで "client connected" ログが出ているか |
| 切替時に AI 表示が一瞬止まる | 正常。WsClient が 2 秒以内に再接続するのを待つだけ |
| 古い run のモデルで全く違う動き | 学習量が足りない run のモデル。`run_ts` が新しい＝ステップ数が多いモデルで確認する |
| 学習中なのにクライアントに何も映らない | `training.train` は WebSocket を持たない。学習しながら見るなら `live_server`（Step 3-b） |

## 学習時間の見積もり

i7-10750H (6 物理コア, n_envs=4) 想定。**`total_timesteps` は安全上限（タイムアウト）で、
既定では Stage 1→4 をステージ予算どおり走り切って終了する**。

| 用途 | 推奨コマンド例 | 目安時間 | 出力モデル数 |
|---|---|---|---|
| 煙テスト（動作確認のみ） | `--no-curriculum --stage-steps 500` | 数秒 | プリセット 1 本 |
| **プリセット生成（既定 Stage 1→4）** | `--n-envs 4` | **約 25 時間**（実測 2 steps/秒） | プリセット 1 本 |
| Stage 5 完走（全形状） | `--n-envs 4 --target-stage 5` | 約 35 時間 | プリセット 1 本 |

ローカルは PyBullet の `settle_duration=2s` が律速で、**実測 約 2 steps/秒**（`time/fps` 中央値 2.0）。
**どの用途でも 1 回の学習で出るモデルは 1 本**（全ステージ走破後のプリセット）。
途中経過が欲しい場合は `--stage-steps` を刻んで複数回に分けて走らせる。

## クラウド学習との関係

このローカル workflow は「**試運転**」用。本番学習は AWS の learner EC2 が
隔週土曜 14-22（**暫定・調整中**）に走り、モデルを S3 に保存する。

ローカルで作ったモデルを AWS のデモ側で再生したい場合は S3 にアップ：

```powershell
$ACCOUNT = aws sts get-caller-identity --query Account --output text
# fresh/ の最大ステップのモデルを最新モデルとして S3 にアップ
$model = & .venv\Scripts\python.exe -c "from block_stacker.training.checkpoint import find_latest_checkpoint; from pathlib import Path; p = find_latest_checkpoint(Path('output/training')); print(p)"
aws s3 cp $model s3://bs-app-$ACCOUNT/models/latest.pt
```

→ 次のクラウドデモ起動時にこのモデルが自動的に読み込まれる。

---

## 日次配信モード（fresh/played 方式）

学習で生成したモデルを `fresh/` に蓄積し、**`advance_day.ps1` を毎日呼ぶだけで
古い→新しい順に自動ステップアップ**して配信する。

```
学習後           → fresh/ に sac_<run_ts>_<steps>_steps.zip（プリセット 1 本）が生成
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
| `serving.live_server` | 学習しながら見たいとき | 配信＋バックグラウンド学習の融合（Step 3-b） |

> これらは**複数 run 分のモデル**を切り替えて再生するツール。1 回の学習で出るモデルは
> 1 本なので、切り替えて見たい場合は学習を複数回走らせて `fresh/` に溜める必要がある。

### セットアップ手順

```powershell
# 1. 学習を実行（Stage 1→4 をステージ予算どおり走り、fresh/ にモデルが 1 本できる）
.venv\Scripts\python.exe -m block_stacker.training.train --n-envs 4

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
      sac_20260713-100000_180000_steps.zip   ← 全ステージ走破後のプリセット（最終モデル）
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
- 配信＋学習融合サーバ: [`src/block_stacker/serving/live_server.py`](../src/block_stacker/serving/live_server.py)
- **ライブ配信モード**: [`docs/live_mode.md`](live_mode.md)（学習しながら配信する `live_server` の起動・n_envs 設定・スナップショット引き継ぎ）
- ヘルパー: [`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1)、[`tools/local_loop.ps1`](../tools/local_loop.ps1)、[`tools/advance_day.ps1`](../tools/advance_day.ps1)
- **ログ解読マニュアル**: [`docs/log_reading.md`](log_reading.md)（学習/推論ログの読み方）
- 設計書: [`docs/block_stacker_design.md`](block_stacker_design.md)（3 層記憶アーキテクチャ §3）
- デプロイ手順書: [`docs/aws_deployment.md`](aws_deployment.md)（§付録 E §1 に記憶仕様の詳細）
