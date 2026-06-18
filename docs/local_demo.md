# ローカル試運転手順書（成長観察デモ）

ローカルで SAC を学習させながら、**timestep ごとの checkpoint** をデモ再生して、AI の
成長過程を観察するためのガイド。

> AWS にデプロイする本番は別途 [`docs/aws_deployment.md`](aws_deployment.md) を参照。
> こちらは試運転・動作確認・成長観察用のローカルワークフロー。

## 概要

```
┌──────────────────────────────────────────────────────────┐
│ 1. 学習を回す                                            │
│    python -m block_stacker.mvp2.train --total-timesteps 100000│
│    → output/mvp2/checkpoints/ に sac_stage1_5000_steps.zip... │
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

```powershell
# 全ステージ（既定。フラグ不要でカリキュラム ON）
.venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 100000

# まず Stage 1→2 だけ試す
.venv\Scripts\python.exe -m block_stacker.mvp2.train --max-stage 2 --n-envs 6 --total-timesteps 100000

# 単一ステージだけ素早く確認したいとき（Stage 1 のみ、約 10 分）
.venv\Scripts\python.exe -m block_stacker.mvp2.train --no-curriculum --n-envs 6 --total-timesteps 100000
```
> 新人 AI は短い予算だと卒業条件に届かず「卒業せず中断」になる。進行そのものを素早く見たいだけなら
> `configs/training.yaml` の `graduation.threshold` を下げる／`ratio` を下げる（目標を低く）。
> これらは **環境変数 `BS_GRADUATION_THRESHOLD` / `BS_GRADUATION_RATIO` / `BS_GRADUATION_WINDOW`**
> でも上書きできる（env var > training.yaml > 既定）。本番（AWS の learner）も既定でカリキュラム ON。

学習中：
- `output/mvp2/checkpoints/sac_stage{N}_<steps>_steps.zip` が `save_freq` ごとに保存される
- `output/mvp2/tb/` に TensorBoard ログが書かれる
- `output/mvp2/sac_final.zip`（最終モデルのみ）が保存される。ステージごとの最終モデルは保存せず、checkpoints/ で補完

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
  0:    5000 steps  (sac_stage1_5000_steps.zip)
  1:   10000 steps  (sac_stage1_10000_steps.zip)
  2:   15000 steps  (sac_stage1_15000_steps.zip)
  ...
 19:  100000 steps  (sac_stage1_100000_steps.zip)

番号を入力 (例: 5)、'all' で全部、'q' で終了
> 0
=== Step 5000 (sac_stage1_5000_steps.zip) ===
  ai_server PID 12345、 60 秒間再生...
```

→ Godot 画面で AI の動きを観察できる。

#### Auto モード（全部順番に）

```powershell
tools\demo_checkpoints.ps1 -Mode auto -Seconds 30
```

20 個の checkpoint を各 30 秒ずつ自動で順番再生。**約 10 分かけて AI の成長を一気に見られる**。

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

| パラメータ | デフォルト | 説明 |
|----------|---------|------|
| `-CheckpointsDir` | `output\mvp2\checkpoints` | checkpoint 保存先 |
| `-Seconds` | 60 | 各 checkpoint の再生時間（秒）|
| `-Mode` | `interactive` | `interactive` (一つ選ぶ) または `auto` (全部順番) |
| `-Python` | `.venv\Scripts\python.exe` | Python 実行パス |
| `-Godot` | `D:\Godot_...\Godot_...exe` | Godot 実行パス |
| `-LaunchGodot` | (未指定なら手動) | このフラグで Godot を自動起動 |

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
| 30,000 | 約 8 分 | 6 個 | 各 60s で 6 分 |
| 100,000 | 約 25 分 | 20 個 | 各 60s で 20 分 |
| 500,000 | 約 2 時間 | 100 個 | auto モード 30s で 50 分 |
| 1,000,000 | 約 4 時間 | 200 個 | auto モード 30s で 100 分 |

→ 最初は `100,000` から試して、感触掴んでから本格学習を回すのがおすすめ。

## クラウド学習との関係

このローカル workflow は「**試運転**」用。本番学習は AWS の learner EC2 が
隔週土曜 14-22 に走り、モデルを S3 に保存する。

ローカルで作ったモデルを AWS のデモ側で再生したい場合は S3 にアップ：

```powershell
$ACCOUNT = aws sts get-caller-identity --query Account --output text
aws s3 cp output\mvp2\sac_final.zip s3://bs-app-$ACCOUNT/models/latest.pt
```

→ 次のクラウドデモ起動時にこのモデルが自動的に読み込まれる。

## 関連

- 学習スクリプト: [`src/block_stacker/mvp2/train.py`](../src/block_stacker/mvp2/train.py)
- 推論サーバ: [`src/block_stacker/mvp3/ai_server.py`](../src/block_stacker/mvp3/ai_server.py)
- ヘルパー: [`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1)
- **ログ解読マニュアル**: [`docs/log_reading.md`](log_reading.md)（学習/推論ログの読み方）
- 設計書: [`docs/block_stacker_design.md`](block_stacker_design.md)（3 層記憶アーキテクチャ §3）
- デプロイ手順書: [`docs/aws_deployment.md`](aws_deployment.md)（§付録 E §1 に記憶仕様の詳細）
