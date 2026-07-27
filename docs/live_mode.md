# ライブ配信モード手順書

`serving/live_server.py` を使った「配信 + バックグラウンド学習の融合」モードの手順書。
設計の詳細は [`docs/block_stacker_design.md`](block_stacker_design.md) §4「ライブ配信モード」参照。

## 目次

- [1. プリセット（初期スナップショット）の生成](#1-プリセット初期スナップショットの生成)
- [2. ライブ配信モードの起動](#2-ライブ配信モードの起動)
- [3. バックグラウンド学習の n_envs 設定（コスト最適化）](#3-バックグラウンド学習の-n_envs-設定コスト最適化)
- [4. シャットダウンとスナップショット引き継ぎ](#4-シャットダウンとスナップショット引き継ぎ)

---

## 1. プリセット（初期スナップショット）の生成

### なぜプリセットが必要か

`live_server.py` はランダム初期化モデルから直接起動できません。
ランダム重みでは積み木を全く積めず、視聴に値する行動が取れないためです。
**標準は Stage 3 のみ 10k steps のプリセット**（掴む・運ぶはできるが積めない「不器用な子供」）。
コンセプト上わざと不出来さを残すので、Stage 4/5 まで仕上げる必要はない。

### 手順: train.py でプリセットを生成する

進行は**固定ステップ制**で、各ステージは `stages[].steps` を消化したら**成績によらず**次へ進みます
（**卒業判定は廃止**。経緯は [`docs/design_change_record.md`](design_change_record.md) §1.2.1）。
走り切った時点でプリセットが `fresh/` に 1 本保存されます。

> **標準は下の 1 本目（Stage 3 のみ）**。ただし**ステージ範囲は既定ではない**ので
> `--start-stage 3 --target-stage 3` の明示が必要（既定は `start=1 / target=4`）。

```powershell
# ---- ライブ用プリセット（標準・Stage 3 のみ 10k＝Stage3 の既定、約1.1h）----
.venv\Scripts\python.exe -m block_stacker.training.train --start-stage 3 --target-stage 3

# ---- 全 Stage 1→4 を回す場合（n_envs は既定 1。合計 165,000 = 約18時間）----
.venv\Scripts\python.exe -m block_stacker.training.train

# ---- Stage 5（全形状）まで（合計 235,000 = 約26時間）----
.venv\Scripts\python.exe -m block_stacker.training.train --target-stage 5

# 生成物（いずれも同じ場所）:
#   output/training/fresh/sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip  ← NN 重み（プリセット1本のみ）
#   output/training/replay_buffer.pkl                               ← 長期記憶
#   output/training/resume_state.json                               ← カリキュラム進捗
```

走り切った時点で `fresh/` へプリセットが保存され、`resume_state.json` に到達ステージが記録されます
（`--target-stage` は「走る範囲の上限」であって、到達で終了する条件ではありません）。

```json
// resume_state.json 例（標準レシピ: Stage 3 のみ 10k）
{
  "num_timesteps": 10000,
  "next_stage_id": 3,
  "completed_stages": [3],
  "timestamp": "2026-07-21T09:41:19"
}
```

```json
// resume_state.json 例（--target-stage 5 で Stage 5 まで走破）
{
  "num_timesteps": 498000,
  "next_stage_id": 5,
  "completed_stages": [1, 2, 3, 4, 5],
  "timestamp": "2026-07-13T14:00:00"
}
```

### 最低限の目安

| プリセット | 位置づけ | 説明 |
|---|---|---|
| **Stage 3 のみ 10k（標準）** | ◎ 本線 | 掴む・運ぶはできるが積めない。Stage 5 の世界で未知形状に手こずる「不出来さ」が残る |
| Stage 4 到達 | ○ | 三角柱まで学習済み。より器用になる |
| Stage 5 到達 | ○（上手すぎ注意）| 全形状習得。コンセプト（不出来さ）とはやや逆方向 |

---

## 2. ライブ配信モードの起動

### 初回起動（プリセット生成直後）

```powershell
.venv\Scripts\python.exe -m block_stacker.serving.live_server `
    --snapshot-dir output/training `
    --n-envs 1 `
    --sync-every 50 `
    --duration 28800
# --n-envs 0 なら配信のみ（学習なし）
# --duration 0 なら無制限
```

> ⚠️ **推奨実行値は `--n-envs 1 --sync-every 50`。コード既定（`--n-envs 4` / `--sync-every 500`）
> とはズレているので、必ず明示すること。**
>
> - **`--n-envs` はスナップショットを作った時の n_envs と一致必須**。学習は `set_env()` で env を
>   差し替えるが、SAC はモデルの n_envs と env 数が違うと `AssertionError` を投げる。
>   このとき**学習スレッドだけが落ちて配信は生き残る**ため、
>   「配信は動いているのに賢くならない」状態に気づきにくい。
>   `configs/training.yaml` の `sac.n_envs`（既定 **1**）で学習したプリセットなら `--n-envs 1`。
>   ログに `[train] background training thread crashed` が出ていないか必ず確認する。
> - **`--sync-every 500`（既定）は反映が粗い**。50 にすると学習成果が 10 倍こまめに表示へ乗る。
> - コード既定値は未変更（このズレは docs 側で吸収している）。

`--snapshot-dir` 配下の `fresh/` または `played/` にある最大ステップ checkpoint を
自動選択します（`find_latest_checkpoint` の `(run_ts, steps)` 降順）。

モデルを明示する場合:

```powershell
.venv\Scripts\python.exe -m block_stacker.serving.live_server `
    --model output/training/fresh/sac_20260713-140000_498000_steps.zip `
    --snapshot-dir output/training `
    --n-envs 1 --sync-every 50
```

### 2 回目以降（スナップショット引き継ぎ）

前回セッション終了時に `_save_live_snapshot` が
`fresh/sac_<run_ts>_<steps>_steps.zip` + `replay_buffer.pkl` + `resume_state.json` を保存します。
次回起動時は `--snapshot-dir` をそのまま指定するだけで自動的に引き継がれます
（`--no-resume` 不要）。

```powershell
# 毎日同じコマンドで OK（スナップショットを自動引き継ぎ）
.venv\Scripts\python.exe -m block_stacker.serving.live_server `
    --snapshot-dir output/training --n-envs 1 --sync-every 50
```

---

## 3. バックグラウンド学習の n_envs 設定（コスト最適化）

### 基本方針: 「ライブ学習はほぼ無料」

配信インスタンスはストリーミングのために常時稼働が必要です。
そのインスタンス上でバックグラウンド学習も行うため、**追加インスタンスコストはほぼゼロ**。
唯一のコストは「CPU 使用率上昇による Spot 強制中断リスクの増加」です。

### インスタンス別の n_envs（コンセプト：ゆっくり育てる）

**このサービスは学習スループットを最大化しない。** `n_envs` は既定 **1**（`configs/training.yaml`、
`gradient_steps` も 1 と揃える）。したがってインスタンス選定は「学習を速くする」ためではなく、
**表示の 240Hz 物理に専有コアを与えて配信を滑らかに保つ**ため。3 段階の選定根拠は
[`block_stacker_design.md`](block_stacker_design.md) §8.3.1。

| デモ instance | 物理コア / RAM | 推奨 `--n-envs` | 表示の滑らかさ |
|---|---|---|---|
| t4g.small（配信専用 EC2） | — | **0** | 配信のみ。学習は載せない |
| 最低 c6a.xlarge | 2 / 8GB | **0〜1** | 学習を載せると重い物理時にフレーム落ち。滑らかさ優先なら 0 |
| **推奨 c6a.2xlarge** | 4 / 16GB | **1** | 表示に1コア専有＋学習で余裕。◎ |
| 最高 m7a.2xlarge | 4 / 32GB | **1** | 単一コア clock 最高で最も滑らか。◎ |

> `--n-envs 0` は「配信のみ・学習なし」。最低構成で表示品質を最優先するときや、
> 学習を別の learner インスタンスに寄せるとき（元の3層設計）に使う。
> `--n-envs` は **スナップショットを作った時の n_envs と一致必須**（既定 1 なら 1）。

> **なぜ n_envs を上げないのか**: 並列を増やせば速く賢くなるが、それはコンセプト（不器用さを
> 残してゆっくり成長を眺める）に反する。速く仕上げたいプリセットは別途 `training.train` で作る。

専用学習は 1 ケタ以上高効率ですが、live_server 上での継続学習は「配信しながらじわじわ賢くなる」
演出に特化した用途であり、純粋な学習効率よりも **長期連続稼働でのゆっくりした成長** を目的としています。

### n_envs の調整基準

- **240Hz physics loop が遅延するようになったら** n_envs を下げる。
  ログに `[broadcaster] frame overrun` 相当の警告が出たら `--n-envs` を 1 減らす。
- **Spot 中断が増えた** と感じたら CPU 使用率を下げるため n_envs を減らす。
- **learning_starts 以前はほぼ CPU ゼロ** (replay buffer に enough samples が溜まるまで学習しない)。
  起動直後のコスト上昇は一時的。

### serving 負荷の内訳

```
asyncio スレッド:
  PhysicsBroadcaster 240Hz  ← serving PyBullet   ~1 vCPU
  ai_driver_task predict()  ← SAC policy.forward  軽量 (<0.1 vCPU)
  WebSocket broadcast       ← asyncio I/O         <0.1 vCPU

live-train スレッド (n_envs=1):
  VecEnv × 1                ← 独立 PyBullet で1 env  ~1 vCPU
  SAC gradient updates      ← train_freq=1, gradient_steps=1   ~0.3 vCPU
```

> つまり融合構成の実働は「表示 ~1 vCPU ＋ 学習 ~1.3 vCPU」。物理2コア（c6a.xlarge）では
> 両者が同じ物理コアを奪い合い、重い物理時に表示がカクつく。表示に1コア専有させるには
> 物理4コア（c6a.2xlarge 以上）が要る（§8.3.1）。

---

## 4. シャットダウンとスナップショット引き継ぎ

### 正常終了（`--duration` 経過）

1. asyncio タスク（physics / ai_driver / serve）を `asyncio.wait_for` でキャンセル。
2. `stop_event.set()` → `LiveCallback._on_step()` が `False` を返し `SAC.learn()` を終了。
3. `_save_live_snapshot()` が `fresh/` + `replay_buffer.pkl` + `resume_state.json` を保存。
4. `_self_stop_instance()` が呼ばれる（EC2 デプロイ時はここに self-stop 実装を差し込む）。

### Ctrl-C / SIGTERM 割り込み

`KeyboardInterrupt` を `main()` がキャッチして正常終了パスを経由します。
`finally` ブロックでスナップショットが保存されるため、強制終了以外はデータ損失なし。

### 強制終了 / Spot 中断

`_training_thread` の `finally` は `train_model is not None` の場合のみスナップショットを試みます。
VecEnv 構築前にクラッシュした場合は保存されません。
Spot 中断の場合は IMDS 中断通知（2 分前）を監視して事前保存することを推奨します（付録 F 参照）。

---

## 関連ドキュメント

- [`docs/block_stacker_design.md`](block_stacker_design.md) §4「ライブ配信モード」— 設計詳細
- [`docs/aws_deployment.md`](aws_deployment.md) — AWS デプロイ手順
- [`docs/local_demo.md`](local_demo.md) — ローカル試運転手順
- [`src/block_stacker/serving/live_server.py`](../src/block_stacker/serving/live_server.py) — ソース
