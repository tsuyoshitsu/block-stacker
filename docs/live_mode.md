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
**最低でも Stage 4（cube + cuboid + triangular_prism）、理想は Stage 5 到達済み**の
checkpoint を用意してから live_server を起動してください。

### 手順: train.py でプリセットを生成する

```powershell
# c6a.4xlarge (16vCPU, AMD) での推奨コマンド（約 1〜2h で Stage 5 に到達）
.venv\Scripts\python.exe -m block_stacker.training.train `
    --n-envs 8 `
    --total-timesteps 500000

# 生成物:
#   output/training/fresh/sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip  ← NN 重み
#   output/training/replay_buffer.pkl                               ← 長期記憶
#   output/training/resume_state.json                               ← カリキュラム進捗
```

カリキュラムが Stage 4 または 5 まで進んだことを `resume_state.json` の
`next_stage_id` で確認してください（4 以上であれば live_server の起動が可能）。

```json
// resume_state.json 例（Stage 5 到達済み）
{
  "num_timesteps": 498000,
  "next_stage_id": 5,
  "completed_stages": [1, 2, 3, 4],
  "timestamp": "2026-07-13T14:00:00"
}
```

### 最低限の目安

| 状態 | live_server 利用可否 | 説明 |
|---|---|---|
| Stage 1-3 のみ | △ (非推奨) | 動作はするが、円柱・三角柱が出現する Stage 5 の世界で機能しない |
| Stage 4 到達 | ○ | 三角柱まで学習済み。円柱の扱いは拾い学習に頼る |
| Stage 5 到達 (推奨) | ◎ | 全 4 形状を学習済み。視聴映えする行動が期待できる |

---

## 2. ライブ配信モードの起動

### 初回起動（プリセット生成直後）

```powershell
.venv\Scripts\python.exe -m block_stacker.serving.live_server `
    --snapshot-dir output/training `
    --n-envs 2 `
    --duration 28800
# --n-envs 0 なら配信のみ（学習なし）
# --duration 0 なら無制限
```

`--snapshot-dir` 配下の `fresh/` または `played/` にある最大ステップ checkpoint を
自動選択します（`find_latest_checkpoint` の `(run_ts, steps)` 降順）。

モデルを明示する場合:

```powershell
.venv\Scripts\python.exe -m block_stacker.serving.live_server `
    --model output/training/fresh/sac_20260713-140000_498000_steps.zip `
    --snapshot-dir output/training `
    --n-envs 2
```

### 2 回目以降（スナップショット引き継ぎ）

前回セッション終了時に `_save_live_snapshot` が
`fresh/sac_<run_ts>_<steps>_steps.zip` + `replay_buffer.pkl` + `resume_state.json` を保存します。
次回起動時は `--snapshot-dir` をそのまま指定するだけで自動的に引き継がれます
（`--no-resume` 不要）。

```powershell
# 毎日同じコマンドで OK（スナップショットを自動引き継ぎ）
.venv\Scripts\python.exe -m block_stacker.serving.live_server `
    --snapshot-dir output/training --n-envs 2
```

---

## 3. バックグラウンド学習の n_envs 設定（コスト最適化）

### 基本方針: 「ライブ学習はほぼ無料」

配信インスタンスはストリーミングのために常時稼働が必要です。
そのインスタンス上でバックグラウンド学習も行うため、**追加インスタンスコストはほぼゼロ**。
唯一のコストは「CPU 使用率上昇による Spot 強制中断リスクの増加」です。

### インスタンス別推奨 n_envs

| インスタンス | vCPU | 推奨 `--n-envs` | 推定スループット | 根拠 |
|---|---|---|---|---|
| t4g.small (配信 EC2) | 2 | 0 | — | 配信専用。serving で CPU 飽和 |
| c6i.xlarge (デモ EC2) | 4 | **2** | ~300–600 steps/sec | serving 2 vCPU + training 2 vCPU |
| c6a.xlarge | 4 | **2** | ~400–700 steps/sec | AMD は vCPU 当たり学習効率がやや高い |
| c6a.2xlarge | 8 | **4–6** | ~800–1400 steps/sec | serving 2 vCPU + training 4–6 vCPU |

> **t4g.small** は WebSocket 配信 + PyBullet 240Hz だけで CPU を使い切るため
> `--n-envs 0`（配信専用）が必須です。

### 学習スループット vs 専用学習の比較

| 運用形態 | インスタンス | n_envs | 推定 steps/8h | コスト/8h (Spot) |
|---|---|---|---|---|
| ライブ学習 (c6i.xlarge, n_envs=2) | $0.17/hr | 2 | ~0.9M–2M steps | 配信コストに込み |
| 専用学習 (c6a.4xlarge) | $0.27/hr | 8 | ~58M steps | +$2.16 |

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

live-train スレッド (n_envs=2):
  SubprocVecEnv × 2         ← 各 env が独立 PyBullet プロセス  ~1 vCPU × 2
  SAC gradient updates      ← train_freq=1, gradient_steps=8   ~0.5 vCPU
```

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
