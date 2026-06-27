# 積み木AI配信サービス 設計書

## 1. プロジェクト概要

**コンセプト**: 積み木を積むAIの成長過程をリアルタイムで配信するサービス

- 24時間稼働、視聴者は隙間時間で成長を楽しむ「アンビエント配信」
- AIは「子供」のメタファーで、不器用さも演出として活かす
- 同時視聴者: 〜15人想定

### 「子供っぽさ」を技術で表現する 3 層記憶

人間（特に子供）の記憶構造を AI にも持たせる：

| 層 | 役割 | 実装 |
|---|------|------|
| **勘** | 体に染み込んだ感覚（明示的に思い出せない） | ニューラルネットの重み |
| **短期記憶** | 「ついさっき」の鮮明な記憶 | 観測辞書に直近 5 手の履歴を同梱 |
| **長期記憶** | 強烈な体験・高く積めた経験は長く覚え、つまらない記憶はすぐ薄れる | 重みつきリプレイバッファ（event 別初期重み + 直前タワー高さ補正 + 時間減衰 + recall ノイズ）|

詳細は §4 で。

---

## 2. アーキテクチャ概要

```
┌─────────────────────────────────────┐
│ 学習 EC2 (c6a.4xlarge, AMD CPU)      │
│ - SAC + 重みつきリプレイバッファ      │
│ - SubprocVecEnv 8 並列 collect       │
│ - 5 分毎 + 完了時 S3 にcheckpoint    │
└──────────────┬──────────────────────┘
               │ PUT
               ▼
        ┌─────────────────┐
        │   S3 Bucket      │
        │  - models/       │
        │  - state/        │
        │  - world_state/  │
        │  - configs/      │
        └─────────────────┘
               │ GET（起動時 + 崩落時）
               ▼
┌─────────────────────────────────────┐
│ デモ EC2 (c6i.xlarge, Intel CPU)     │
│ - ai_server.py (PyBullet 物理シム)   │
│ - 物理1x速、フルアニメーション         │
│ - WebSocket :8765 で配信             │
└──────────────┬──────────────────────┘
               │ 内部 VPC (SG 制限)
               ▼
┌─────────────────────────────────────┐
│ 配信 EC2 (t4g.small ARM, Caddy)      │
│ - 自動 TLS (Let's Encrypt)           │
│ - reverse_proxy wss://→ デモ:8765   │
└──────────────┬──────────────────────┘
               │ WebSocket (wss)
               ▼
        Godot 4.4 .NET クライアント (C#) × ~15
```

### サーバ構成

| コンポーネント | 役割 |
|---|---|
| 学習 EC2 (c6a.4xlarge Spot) | 並列環境で SAC 学習、S3 にチェックポイント |
| デモ EC2 (c6i.xlarge Spot) | 1x速で物理シム実行、WebSocket 配信 |
| 配信 EC2 (t4g.small Spot, ARM) | Caddy 自動 TLS + リバプロでクライアントへブロードキャスト |
| S3 | モデル重み + 持続ワールド状態 + configs |
| VPC Endpoints | S3 Gateway (無料) + ECR Interface × 2 + Logs Interface (Private Subnet 用) |

### モデル共有フロー

1. 学習側: 5 分毎に `s3://bucket/models/` へ checkpoint sync
2. デモ側: 起動時に S3 から最新モデルを取り込み、ai_server で推論

> **設計変更履歴**:
> - ElastiCache Redis は未使用のため撤去（実装上 import 無し、月 ¥2,460 節約）
> - 学習を GPU (g4dn) → CPU (c6a) に変更（NN が小さく PyBullet が CPU bound なため）

---

## 3. 通信プロトコル

### 同期方式: スリープ/ウェイク明示モデル

- PyBullet の sleeping island 機構を利用
- AWAKE blocks のみが 60Hz で送信される
- ASLEEP になったブロックは最終ポーズで確定、以降送信しない
- 衝突等で再び動き出したら wake_event を送信

### メッセージ種別（type byte で判別）

| type | 名称 | 内容 |
|---|---|---|
| 0x01 | world_config | 接続時: 全ブロック静的情報 + work_area + ground info |
| 0x02 | initial_state | 接続時: 全ブロック現ポーズ + AWAKE/ASLEEP |
| 0x03 | snapshot | 60Hz: AWAKE blocks のポーズ |
| 0x04 | sleep_event | 単発: ブロックが ASLEEP になった |
| 0x05 | wake_event | 単発: ブロックが AWAKE になった |
| 0x07 | heartbeat | 1Hz: 生存確認 + サーバ時刻同期 |
| 0x08 | collapse_event | 単発: 崩落判定発火（視覚効果フック） |

### snapshot形式 (0x03)

```
[type:u8 = 0x03]
[timestamp:f64]
[seq:u32]
[num_awake:u8]
  [block_id:u16, px:f32, py:f32, pz:f32, qx:f32, qy:f32, qz:f32, qw:f32] × num_awake
```

座標系: **サーバ側は PyBullet の Z-up**。クライアント側 (Godot) が受信時に Y-up に変換。

### 接続シーケンス

```
1. C → S: (optional) hello { client_version }   ← OPEN 後に送信
2. S → C: world_config { blocks[].shape/type/dims/color, work_area, ground{size}, boundary{}, ... }
3. S → C: initial_state { all_blocks[].pose, all_blocks[].awake_flag }
4. (以後) S → C: snapshot @60Hz + events as they happen + heartbeat @1Hz
```

### タイムスタンプ仕様

- サーバ起動時を 0.0 とする単調増加秒（f64）
- 壁時計ではなく単調時間 → 視聴側で時刻同期不要
- クライアントは古い/重複フレームを破棄するために使用

---

## 4. AI/RL設計

### アルゴリズム

- **SAC** (Stable-Baselines3)
- Off-policy + replay buffer + 自動エントロピー温度調整
- 「子供メタファー」と SAC の最大エントロピー方策（多様な行動を積極的に試す）が整合
- 並列環境 (SubprocVecEnv n_envs=8) で collect を高速化

> **設計判断履歴**: 一時 PPO に切替えたが、`「過去のプレイを思い出しながら学ぶ」` 哲学のため SAC に復帰。
> その過程で「短期記憶を観測に追加する」設計が固まり、SAC + 短期記憶 + 重みつきリプレイバッファの 3 層構成に到達。

### 3 層記憶アーキテクチャ

#### 1. 勘 = ニューラルネットの重み

| ストリーム | 入力 | 処理 | 出力次元 |
|---------|------|------|--------|
| blocks | (max_blocks, per_block_dim) | Set Transformer (2 layers, 4 heads) | 64 |
| heightmap | (4, 32, 32) | 小型 CNN | 32 |
| tower_top_z | (1,) | Linear | 16 |
| **短期記憶** | recent_actions/rewards/results/mask | MLP | 16 |
| 連結 + 融合 | 128 | Linear + ReLU | 128 → SAC policy / value head へ |

実装: [`src/block_stacker/policy/feature_extractor.py`](src/block_stacker/policy/feature_extractor.py) `HybridFeatureExtractor`

#### 2. 短期記憶 = 観測辞書に直近 5 手を同梱

```python
observation = {
    "blocks": ...,
    "blocks_mask": ...,
    "heightmap": ...,
    "tower_top_z": ...,
    "recent_actions": (5, 7),    # 直近 5 手の action
    "recent_rewards": (5,),       # 直近 5 手の報酬
    "recent_results": (5,),       # 直近 5 手のイベント種別スコア
    "recent_mask": (5,),          # 有効フラグ（episode 序盤は 0）
}
```

実装: [`src/block_stacker/env/env.py`](src/block_stacker/env/env.py) の `_stm_*` deque、episode reset でクリア。

#### 3. 長期記憶 = 重みつきリプレイバッファ

実装: [`src/block_stacker/policy/weighted_replay_buffer.py`](src/block_stacker/policy/weighted_replay_buffer.py)

| 機構 | 内容 |
|------|------|
| **イベント種別の初期重み** | 崩落 1.0 > 失敗 0.7 > 新記録 0.5 > 成功 0.3 > 無駄手 0.1 |
| **直前タワー高さ補正** | 初期重み ×= clip(1 + coef×(height_before/reference), 1, max_factor)。高いタワーで起きた経験ほど強い記憶（掛け算なので event 順位は保たれる、`memory_system.height_weighting`） |
| **時間で減衰** | 1 step ごとに ×0.9999 (半減期 ~6,900 step) |
| **重みつき抽選** | 重要な記憶ほど学習で参照されやすい |
| **読み出しブレ** | blur = max_blur × (1 - 現在重み)。古い記憶ほど曖昧 |
| **eviction** | 容量上限時、K=16 トーナメントで重み最小のスロットを上書き |

#### Event type 判定 (env.step() で算出)

| 優先 | event | 条件 | 初期重み |
|----|-------|------|--------|
| 1 | `collapse` | 崩落判定 trigger | 1.0 |
| 2 | `failure` | 進歩なし truncate | 0.7 |
| 3 | `height_record` | タワー高さ新記録 | 0.5 |
| 4 | `success` | placement 成功（記録未更新）※報酬は「置いた高さ」で補正（§報酬） | 0.3 |
| 5 | `no_progress` | 上記いずれにも該当しない | 0.1 |

#### 学習の引き継ぎ（--resume）

`--resume` フラグで前回の学習状態を引き継いで続きから学習できる。引き継ぐのは **勘** と **長期記憶**
の 2 層のみ。短期記憶（recent_* deque）は `env.reset()` で自動クリアされるため何もしない（設計通り）。

| 引き継ぐもの | 方法 | 減衰 |
|------|------|------|
| **勘（NN重み）** | `sac_final.zip` から `SAC.load()` | そのまま（無加工） |
| **長期記憶（リプレイバッファ）** | `replay_buffer.pkl` から `load_replay_buffer()` | 経過日数 × `steps_per_day` だけ `global_step` を加算し、全記憶の age を増やして重みを一律減衰 |

**減衰の仕組み（Method A）**: `replay_buffer.global_step += elapsed_steps` で
全スロットの `age = global_step - birth_steps` が同時に増加し、現在重み = `initial_w × decay_rate^age`
が一斉に古くなる。強い記憶（`collapse` 初期重み 1.0）は弱い記憶（`no_progress` 0.1）
より長く生き残る（`weight_floor` が下限）。

学習完了時に `replay_buffer.pkl` と `resume_state.json` が `output/mvp2/` に**毎回**保存される。
`resume_state.json` には `num_timesteps`, `next_stage_id`, `completed_stages`, `timestamp` が記録される。

設定（`configs/training.yaml` の `resume:` セクション）:

| キー | 既定 | 説明 |
|-----|------|------|
| `steps_per_day` | 5000 | 1 日あたりの減衰換算ステップ数（`0.9999^5000 ≈ 0.607`、≈1.4 日で半減） |
| `elapsed_days` | null | 経過日数を手動指定（null = timestamp 差から自動算出） |
| `elapsed_steps` | null | ステップ数を直接指定（最優先、null = 未使用） |

### 観測空間 (Dict)

#### ブロックストリーム

```
per_block_vector = [
  pos_x, pos_y, pos_z,
  quat_x, quat_y, quat_z, quat_w,
  distance_to_tower_base,
  is_in_tower_flag,                 # 散布のみを入れるので常に 0（枠の形は維持）
  exp(-distance_to_tower_base / scale),
  shape_type_one_hot[K],            # K = 4 (cube/cuboid/cylinder/triangular_prism)
  bbox_w, bbox_h, bbox_d,
]
```

近接基準点: **タワー根本（base）**

**枠に入れるのは「拾える散布ブロックの近い順トップ `max_blocks`（=8）」だけ**。積まれた
ブロックは per-block には含めず heightmap が山として表現する。遠い散布ブロックは枠から
あぶれて見えない＝**子供の狭い視野**メタファー。この設計により**世界の合計ブロック数は
`max_blocks` を超えてよい**（同時に映る近い散布ブロックが最大 8 個、という意味）。
近いブロックを使って減ると、遠かったブロックが順に枠へ入ってくる。

> **NaN 姿勢の除外**: 物理が一時破綻して姿勢が NaN/Inf になったブロックは per-block 枠に
> 入れず除外し、有効な散布ブロックだけを観測する（mask も valid 分だけ立つ）。残った配列値は
> 安全クリップ `_sanitize`（NaN/Inf→0/±50 に置換しクリップ）を最終防御として通す。

#### ハイトマップストリーム (4ch × 32 × 32)

- ch 0: 高さ z(x, y)
- ch 1: 勾配 ∂z/∂x
- ch 2: 勾配 ∂z/∂y
- ch 3: 勾配の大きさ |∇z|

### 行動空間: 7次元連続

```
Action = (
  pickup_query_xyz,   # 「この座標に最も近い散乱ブロックを拾う」
  place_xyz,          # 配置目標位置
  place_yaw           # 配置時回転（cube は無視、他形状は影響あり）
)
```
全て [-1, 1] に正規化。

### 報酬 (configs/reward.yaml)

```yaml
place_success: 1.0
height_record: 0.5
collapse: -5.0
time_penalty: -0.05
timeout_penalty: -1.0
collapse_height_threshold: 0.075
reset_height_threshold: 0.025
flatness_bonus: 0.0
flatness_scale: 0.1
```

各変数の意味（変数名だけだと分かりづらいので日本語で）:

| 変数 | 値 | 日本語の意味 |
|---|---|---|
| `place_success` | 1.0 | 拾ったブロックがタワー（縦連結成分）に加わった時の加点の**最大値**。ただし**「置いた高さ」で補正**して与える（後述の案A）。接地レベルに横付けしただけ（持ち上がり≈0）はほぼ 0、上段に積むほど満点に近づく。→ **平たく集める／崩れた分を拾い直す戦略の旨味を消し、上へ積む勾配**を作る |
| `height_record` | 0.5 | タワー高さが**過去最高を更新**した時の加点。無進歩カウンタ `steps_since_progress` をリセットするのもこの「新記録」だけ |
| `collapse` | -5.0 | **崩落**（高さが H_high→H_low へ崩れ＋散らばり）時のペナルティ。即エピソード終了。SAC＋重みつき記憶で長く参照されるため -10 まで強くしなくてよい |
| `time_penalty` | -0.05 | **毎手**の小ペナルティ。何もしない／無意味な運搬を抑制（0.01→0.05 に強化） |
| `timeout_penalty` | -1.0 | 無進歩で **truncate（打ち切り）** した時の追加ペナルティ |
| `collapse_height_threshold` | 0.075 | 崩落判定の **H_high** 既定（ステージごとに上書き） |
| `reset_height_threshold` | 0.025 | 崩落判定の **H_low** 既定（ステージごとに上書き） |
| `flatness_bonus` / `flatness_scale` | 0.0 / 0.1 | **stub（未配線）**。env の reward 計算に未統合 |

**place_success の高さ補正（案A）** — `env._compute`（step 内）で次のように `place_success` を高さで掛ける:

```
block_lift   = 置いたブロックの重心 z − その形状の接地時重心高さ(_spawn_height)   # 接地なら ≈ 0
height_factor = clip(block_lift / 在庫満積み高さ, 0, 1)                            # 0〜1
reward       += place_success × height_factor
```

接地横付け→ ≈0、上段ほど大きい。例（cube・満積み 0.74m）: 接地=0.00 / 1段=0.07 / 数段=0.27 / 満積み相当=0.97。
これにより「一か所に集める」「失敗後に崩れた分を拾い直す（無意味な運搬）」の報酬が消え、勾配が**上へ積む**方向を向く。

### マニピュレーション (階層化)

- **上位ポリシー**: 学習対象（7 次元連続行動）
- **下位ポリシー**: 決定論「ソフト追従キャリア」方式
  - 不可視のキャリア点が 3 phase ベジェ曲線で目標まで移動
  - PyBullet `createConstraint` で position-only 拘束
  - 移動中も物理演算 ON、衝突あり、拘束力 8N 上限
  - → 「子供らしい不器用さ」が自然に表現される

### ブロック状態マシン

```
[scattered/idle]  ← dynamic、地面に静止
    ↓ AI が pickup_query で指定
[transporting]    ← dynamic + soft constraint to carrier
    ↓ 3-phase trajectory 完了
[placing]         ← 拘束解除、純粋な dynamic で落下・着地
    ↓ 一定時間静止
[settled in tower] or [scattered/idle]
```

### カリキュラム学習

目標高さは固定値ではなく **「在庫を全部縦積みした理論高さ × ratio」** で動的に決まる
（`graduation.ratio`、既定 0.6）。在庫が増えれば目標も比例して上がる。

| Stage | 形状 | 在庫（合計） | 満積み高さ | 目標高さ（×0.6） | 難易度 |
|---|---|---|---|---|---|
| 1 | cube | 8（8） | 0.40 | **0.240** | ウォームアップ |
| 2 | cube | 15（15） | 0.75 | **0.450** | スケール拡大 |
| 3 | cube + cuboid | 8+7（15） | 0.68 | **0.408** | 2 形状目、向きが意味を持つ |
| 4 | + **triangular_prism** | 5+5+5（15） | 0.70 | **0.420** | 3 形状、平面安定 |
| 5 | + **cylinder** | 4+4+4+3（15） | 0.74 | **0.444** | **4 形状、転がる円柱は最難** |

> 在庫は観測枠（`max_blocks=8`）より多い（合計最大15個）。観測は「拾える散布ブロックの
> 近い順トップ8」だけを映す（積まれた分は heightmap が表現）ので、合計が枠を超えても破綻しない
> ＝「子供の狭い視野」設計（§観測空間 参照）。在庫を増やしたことで後段の到達余裕も確保。

**順序の根拠**: 「平面で安定する形状 → 転がる形状」。円柱が最後に来るのは、これまで積んだ形状の上に転がりやすい円柱を載せる高度な戦略が求められるため。

> 設計変更履歴: 旧版は cylinder → triangular_prism だったが、円柱の方が難しいため逆転。

#### Stage 卒業条件

```yaml
graduation:
  rule: "success_rate"
  window: 30                  # 成功率を見る直近エピソード数
  threshold: 0.6              # この成功率で卒業
  ratio: 0.6                  # 目標高さ = 在庫満積み高さ × ratio
demotion_enabled: false       # Stage ダウンなし、一方向のみ進行
```

`window` / `threshold` / `ratio` は**コンテナ環境変数**でも上書きできる（優先順位:
env var > training.yaml > 既定）。本番（Docker/ECS）で `-e BS_GRADUATION_RATIO=0.7` 等。
詳細は[`docs/aws_deployment.md`](aws_deployment.md) のデプロイ節。

#### 卒業の判定（実装）

卒業は次のどちらか（**OR**）：

1. **散布0で即卒業**（fast-track）: あるエピソードで散布0（全ブロックを縦タワーに積み切った）を
   達成したら、成功率を待たず**その瞬間に卒業**。env が `info["all_placed"]` を出し、True を見たら即卒業。
2. **目標高さ到達の成功率で卒業**: 「目標高さ到達」の成功率が直近 `window` で `threshold`（既定 0.6）
   以上 → 卒業。env が `info["is_success"]` を出し、`GraduationCallback` が done ごとに集計。

```
is_success = tower_best_height >= 目標高さ   # 成功率の対象（②）。散布0 は含めない
all_placed = 散布0 を達成                     # 即卒業のトリガー（①）
目標高さ   = 在庫満積み高さ × ratio
```

> タワー判定が縦連結のみなので、平たく寄せ集めた塊は「散布0」にも「目標高さ」にもならず成功しない
> （下記「タワーの定義」参照）。`stable_duration` の継続判定は未実装。
>
> **散布0 検出の堅牢化**: 散布0 は「現在のタワーが全ブロックを含む」を positive 確認する
> （`len(blocks) == len(tower)`）。`find_nearest_excluding` が None を返しただけ（＝拾える散布が
> 無い）を散布0 とみなさない。これにより物理破綻で姿勢が NaN 化したり `prev_tower_ids` が陳腐化
> した時に、**低い高さのまま誤って即卒業するのを防ぐ**（この取り違えが過去、最難ステージが数百手で
> 偽卒業する原因だった）。

#### エピソードタイムアウト

- 最大ステップ数: 30
- 高さが更新されない行動が連続 15 回でタイムアウト（failure 扱い）

#### 実装状況（オートカリキュラム）

**実装済み**（[`mvp2/train.py`](src/block_stacker/mvp2/train.py) + [`mvp2/curriculum.py`](src/block_stacker/mvp2/curriculum.py)）。

- **既定で** Stage 1→N を自動進行（`--no-curriculum` で Stage 1 のみに切替）。
- `GraduationCallback` が成功率 ≥ `threshold` を満たすと `learn()` を早期終了し次ステージへ。
- 観測空間は全ステージ共通（`max_blocks=8` 等で固定）なので、**同じ NN・記憶バッファを
  `model.set_env()` で引き継いだまま** env だけ差し替える。タイムステップ計数・TensorBoard も連続。
- 保存: **最終モデル `sac_final.zip` のみ**（ステージごとの最終モデルは保存せず checkpoints/ で補完）。
- `--total-timesteps` は**全ステージ合計の上限（グローバル予算）**。総手数は必ずこの値以下になり、
  早く卒業した残り手数は次ステージへ回る。使い切ったら（卒業しきれず）中断。
- **最終ステージ卒業後も予算が残っていれば継続**（`train.py` の post-loop ブロック）:
  `GraduationCallback` が `learn()` を早期終了させるため最終ステージ卒業時に checkpoint が
  欠落することを防ぐ。ループ後に `model.num_timesteps < total_timesteps` なら最終ステージ環境を
  再構築し `reset_num_timesteps=False` で続きを走らせる（checkpoint が `total_timesteps` まで埋まる）。
- **デモ配信（[`mvp3/ai_server.py`](src/block_stacker/mvp3/ai_server.py)）は常に最終ステージ**
  （全形状）でモデルを動かす。既定モデルは `sac_final.zip`→`sac_stage1_final.zip` の順で自動選択。

### Stage 情報の取り扱い

クライアントには非公開。視聴者は「だんだん上手くなる」というだけの認識。

### 学習エピソード初期状態

**フェーズ 1 (MVP)**: 毎エピソード単純シャッフル (`simple_shuffle`)

```yaml
episode_reset_strategy: "simple_shuffle"
```

---

## 5. ワールド / 物理

### ワールド境界

- `invisible_walls`（見えない壁で作業エリアを囲む）
- 物理バグでの脱走は受容（次回シャッフルまで放置）

### 配置リセット (シャッフル)

- N 回崩落で発火（デフォルト N=3）
- アニメーション: **instant**（瞬間テレポート）
- シャッフル中は AI 停止
- 全散乱ブロック対象、迷子ブロックも回収

### タワーの定義

```
タワー = 地面に接触し、かつ他のブロックの上に「縦に積まれて」連続接続している
        ブロック群の中で、最も高い接続成分（連結成分）
```

- PyBullet の `getContactPoints` で接触グラフを構築 → 連結成分分解 → 地面接続成分から最高 Z 選択
- **縦連結のみ**: 接触法線の Z 成分でフィルタし、上下の積み重なり（と斜面）だけをエッジにする
  （`VERTICAL_NORMAL_MIN=0.5`, `env/tower.py`）。
  - 横並びの側面接触（normal_z≈0）はエッジにしない → **平たく寄せ集めた塊は 1 タワーにならない**。
  - 45°斜面の接触（normal_z≈0.707）は縦連結に含む（斜面に乗ったブロックは積んだ扱い）。
- 更新タイミングは「ブロックが新たに settle した時」と「sleep_event 発火時」のみ

### 崩落判定

H_high 到達フラグを立て、H_low 以下になった時に追加判定で確定：

```python
if current_height >= H_high:
    collapse_armed = True
if collapse_armed and current_height <= H_low:
    if tower_dispersion_ratio_exceeded() and not placing_in_progress():
        fire_collapse_event()
        collapse_armed = False
```

### 物理エンジン

**PyBullet** を採用
- Sleeping island 機構を有効化
- 形状: cube / cuboid / cylinder / **triangular_prism** (直角二等辺三角柱)

### 三角柱の実装

PyBullet には組込プリミティブが無いので `GEOM_MESH` で凸包メッシュとして実装。

| 項目 | 値 |
|------|---|
| 頂点数 | 6（断面 3 点 × 前後 2 面） |
| 面数 | 8（三角形 2 + 矩形 3 を 2 三角形ずつに分割） |
| 軸 | X 軸沿い |
| 断面 | YZ 平面、直角二等辺三角形 |
| 安定姿勢 | y-leg rectangle が下、centroid は底面から leg/3 |

実装: [`src/block_stacker/sim/blocks.py`](src/block_stacker/sim/blocks.py) `_triangular_prism_vertices` / `_triangular_prism_indices`

### 物理パラメータ概要

詳細は `configs/physics.yaml` 参照。

| 項目 | 値 | 根拠 |
|---|---|---|
| 内部レート | 240Hz | 積み木スケールでコンタクト安定 |
| ソルバー反復 | 100 | 高層タワーの安定性 |
| use_split_impulse | true | 積み重ねの安定化 |
| 摩擦 (block-block) | 0.45 | 固着緩和（0.6→0.45, ×0.75） |
| 摩擦 (block-ground) | 0.8 | 土台固定 |
| 摩擦 (block-wall) | 0.4 | 迷子を中央寄せ |
| 反発 (block) | 0.1 | 跳ねない |
| 反発 (wall) | 0.3 | 軽く弾く |
| 重力 | -9.81 m/s² (Z-up) | 標準 |

### ソフト追従キャリア拘束

| 項目 | 値 |
|---|---|
| 拘束タイプ | point2point |
| 最大力 | 8.0 N |
| 軌道速度 | 0.3 m/s |
| 軌道形 | 3 段階（持ち上げ → 水平 → 降下） |
| 持ち上げ高さ余裕 | 0.05 m |

---

## 6. クライアント

### 技術スタック

**Godot 4.4.1 .NET 版 + C#**

- 単一スクリプト `WsClient.cs` で WebSocket + プロトコルデコード + 描画
- `block-stacker-client.csproj`: `Godot.NET.Sdk/4.4.1`, .NET 8 ターゲット

> 設計判断: 当初 GDScript で実装したが、業界一般言語・型安全性・将来移植性を理由に C# に移行。

### ファイル構成

```
client/
├── project.godot                     # features: 4.4 + C#
├── block-stacker-client.csproj       # Godot.NET.Sdk/4.4.1 + net8.0
├── scenes/
│   └── main.tscn                     # Node3D + Camera + Light + Env + WsClient
└── scripts/
    └── WsClient.cs                   # 約 400 行、全機能
```

### 座標系変換

PyBullet (Z-up) ↔ Godot (Y-up) の変換を `ReadPoseTransform` で実施：

```csharp
// X 軸まわり -90°: (x, y, z) → (x, z, -y)
position = new Vector3(px, pz, -py);
qGodot = Quaternion(X, -90°) * qPybullet;
```

cylinder のみ追加で X 軸まわり +90° の補正を per-instance に適用（Godot CylinderMesh の軸 Y を PyBullet の軸 Z に合わせるため）。

### 描画 (4 形状 + 地面)

| 形状 | Mesh | 備考 |
|------|------|------|
| cube / cuboid | Godot 標準 `BoxMesh` | dims そのまま渡す |
| cylinder | Godot 標準 `CylinderMesh` | 軸補正 (per-instance Transform) |
| **triangular_prism** | 自前 `ArrayMesh` | サーバと同じ頂点・面、per-face 法線で flat shading |
| 地面 | `BoxMesh` 3m × 0.02m × 3m | 灰色 (0.4, 0.4, 0.4)、Y=-0.01 |

形状ごとに `MultiMeshInstance3D` を生成し、同じ形状のブロックをバッチ描画。

### マテリアル

- shape ごとに `StandardMaterial3D`（`AlbedoColor` でブロック色）
- `MultiMeshInstance3D.MaterialOverride` で MultiMesh 全体に適用
- `Roughness = 0.6`, `Metallic = 0.0`（プラスチック調）

### 環境光

`main.tscn` の WorldEnvironment に `Environment` リソース：

```
background_color   = (0.15, 0.18, 0.22)   # 落ち着いた暗色
ambient_light      = (0.85, 0.88, 0.95) × 0.5  # 影の面も視認可能に
DirectionalLight   = energy 1.2 + shadow
```

### 接続状態 UI

`CanvasLayer + Label` を `WsClient.cs` でプログラマティック生成。サーバ未接続時に画面中央に表示：

```
サーバとの通信を試行中...
```

ドット数が 0.5 秒ごとに 0→3 で循環するアニメーション付き。OPEN 状態になると自動的に非表示。

### 既定設定

```csharp
[Export] public string ServerUri = "ws://127.0.0.1:8765";  // Windows IPv6 解決の遅延を避けて 127.0.0.1
[Export] public float AutoReconnectSeconds = 2.0f;
[Export] public string ConnectingText = "サーバとの通信を試行中";
```

本番デプロイ時は Inspector で `wss://bs.example.com/` 等に変更。

### フレーム管理

各 snapshot の timestamp を保持、古い/重複フレームは破棄（順序検証）。

### グリッパー表現

なし。AI は物理グリッパーを持たず、運搬中のブロックも視覚的に区別しない。

### UI

接続状態 UI 以外なし。ステータス・デバッグ・ステージ番号など全て非表示。

### カメラ

main.tscn で固定（orbit は今は不採用）：

```
Camera3D position: (1.5, 1.0, 1.5)
Camera3D target: 原点付近
fov: 50
```

---

## 7. 設定ファイル

| ファイル | 内容 | 配置先 |
|---|---|---|
| `world.yaml` | 境界、形状、在庫、シャッフル | サーバ |
| `physics.yaml` | 摩擦、反発係数、シミュレーション周波数、sleep閾値、キャリア拘束 | サーバ |
| `training.yaml` | カリキュラム、SAC ハイパラ、`memory_system`、`short_term_memory` | 学習サーバのみ |
| `reward.yaml` | 報酬係数 | 学習サーバのみ |

### world.yaml 例

```yaml
work_area:
  x_range: [-1.0, 1.0]
  y_range: [-1.0, 1.0]
  z_max: 3.0

boundary:
  type: "invisible_walls"
  restitution: 0.3

ground:
  size: [3.0, 3.0]
  friction: 0.8
  restitution: 0.1

# 4 形状サポート
shapes:
  cube:
    type: "box"
    dims: [0.05, 0.05, 0.05]
    density: 400
    color: [0.9, 0.5, 0.3, 1.0]
  cuboid:
    type: "box"
    dims: [0.08, 0.04, 0.04]
    density: 400
    color: [0.4, 0.7, 0.9, 1.0]
  triangular_prism:
    type: "triangular_prism"
    dims: [0.05, 0.05]          # leg_length, prism_length
    density: 400
    color: [0.95, 0.85, 0.2, 1.0]
  cylinder:
    type: "cylinder"
    dims: [0.025, 0.06]
    density: 400
    color: [0.6, 0.9, 0.5, 1.0]

inventory:
  cube: 4
  cuboid: 2
  triangular_prism: 2
  cylinder: 2

initial_scatter:
  exclude_radius_from_center: 0.15
  min_inter_block_distance: 0.07
  random_yaw: true

shuffle:
  trigger_collapses: 3
  animation: "instant"
  ai_pause_during_shuffle: true
```

### training.yaml 例（抜粋）

```yaml
episode_reset_strategy: "simple_shuffle"

episode:
  max_steps: 30
  max_actions_without_progress: 15
  timeout_treated_as: "failure"

curriculum:
  graduation:
    rule: "success_rate"
    window: 30        # env BS_GRADUATION_WINDOW で上書き可
    threshold: 0.6    # env BS_GRADUATION_THRESHOLD で上書き可
    ratio: 0.6        # 目標高さ=在庫満積み×ratio。env BS_GRADUATION_RATIO で上書き可
  demotion_enabled: false
  # 目標高さは ratio から動的算出するので stage に target_height は持たない。
  stages:
    - id: 1
      name: "Stage 1: cube only, low target"
      shapes_allowed: [cube]
      inventory: {cube: 8}
      ...
    - id: 4
      name: "Stage 4: cube + cuboid + triangular_prism"
      shapes_allowed: [cube, cuboid, triangular_prism]
      inventory: {cube: 5, cuboid: 5, triangular_prism: 5}
      ...
    - id: 5
      name: "Stage 5: + cylinder (最難)"
      shapes_allowed: [cube, cuboid, triangular_prism, cylinder]
      inventory: {cube: 4, cuboid: 4, triangular_prism: 4, cylinder: 3}
      ...

sac:
  total_timesteps: 4000         # 週次配信標準（checkpoint_splits=5 で 800 刻み 5 本）
  n_envs: 8                     # c6a.4xlarge 物理コア飽和
  buffer_size: 50000
  learning_starts: 200
  batch_size: 256
  learning_rate: 0.0003
  tau: 0.005
  gamma: 0.99
  train_freq: 1
  gradient_steps: 8
  ent_coef: "auto"
  target_update_interval: 1
  log_interval: 4
  checkpoint_splits: 5          # total_timesteps を等分した地点でcheckpointを保存
  features_dim: 128

# 重みつきリプレイバッファの設定
memory_system:
  enabled: true
  initial_weights:
    collapse: 1.0
    failure: 0.7
    height_record: 0.5
    success: 0.3
    no_progress: 0.1
  decay_rate: 0.9999
  recall_noise:
    enabled: true
    coordinate_sigma: 0.05
  eviction: "min_weight"
  eviction_tournament_k: 16
  weight_floor: 0.001
  height_weighting:               # 直前タワー高さで初期重みを底上げ（高いほど強い記憶）
    enabled: true
    coef: 1.0                     # 補正の強さ（0 で無効）
    reference: 0.10               # 高さ正規化の基準（典型的な到達高さの目安）
    max_factor: 3.0               # 補正倍率の上限

# 短期記憶（観測辞書に履歴を同梱）
short_term_memory:
  enabled: true
  length: 5

observation:
  max_blocks: 8
  heightmap_resolution: 32
```

### reward.yaml 例

```yaml
place_success: 1.0            # 「置いた高さ」で補正して加点（接地横付け≈0、上段ほど満点）。詳細は §報酬
height_record: 0.5
collapse: -5.0                # 重みつき記憶に長く残るので -10 → -5 に緩和
time_penalty: -0.05           # 0.01→0.05: 平置き/無意味運搬を強く抑制
timeout_penalty: -1.0
collapse_height_threshold: 0.075
reset_height_threshold: 0.025
flatness_bonus: 0.0           # stub、env に未配線
flatness_scale: 0.1
```

---

## 8. AWS構成・運用

### 8.1 リージョン

ap-northeast-1 (Tokyo)

### 8.2 運用スケジュール（4 系統に分離）

| Scheduler | Cron (UTC) | JST 時刻 | 月間時間 | 対象 ASG |
|----------|---------|---------|---------|---------|
| **bs-learner-start** | `cron(0 5 ? * SAT#2,SAT#4 *)` | 第 2/4 土 14:00 | 学習 16h/月 | bs-learner-asg |
| **bs-learner-stop** | `cron(0 13 ? * SAT#2,SAT#4 *)` | 第 2/4 土 22:00 | 同上 | 同上 |
| **bs-demo-start** | `cron(0 5 ? * MON-FRI *)` | 月-金 14:00 | デモ 176h/月 | bs-demo-asg + bs-streamer-asg |
| **bs-demo-stop** | `cron(0 13 ? * MON-FRI *)` | 月-金 22:00 | 同上 | 同上 |

> 設計変更履歴: 旧版は全 ASG が土日 14-22 一括稼働 (68h/月)。新版は学習を絞り、配信を増やして視聴機会 2.6 倍に。

#### Lambda 構成

1 ペア (`bs-scale-up` / `bs-scale-down`) を共有し、各 Scheduler の `input` payload で対象 ASG を指定：

```json
{ "asg_names": ["bs-learner-asg"] }
{ "asg_names": ["bs-demo-asg", "bs-streamer-asg"] }
```

handler.py の `_resolve_asg_names(event)` が payload 優先、未指定なら env var `ASG_NAMES` フォールバック。

祝日は `jpholiday.is_holiday()` で skip（学習・デモ両方とも）。

### 8.3 インスタンス構成

| 役割 | インスタンス | 購入方式 | スペック |
|---|---|---|---|
| 学習 | c6a.4xlarge | **Spot** | 16 vCPU (8 物理コア) / 32GB / AMD EPYC CPU-only |
| デモ | c6i.xlarge | **Spot** | 4 vCPU / 8GB / Intel CPU |
| 配信 | t4g.small (ARM) | **Spot** | 2 vCPU / 2GB + Caddy |

> 設計変更履歴: 学習を GPU (g4dn) → CPU (c6a) に変更。NN が小規模で PyBullet が CPU bound なため GPU が活かせていなかった。月 ¥3,600 → ¥480 に削減。

### 8.4 ネットワーク

```
                Internet
                    │
                    ▼
              Route 53 → EIP
                    │
                    ▼
  ┌───────────────────────────┐
  │ 配信 EC2 t4g.small Spot    │ Public Subnet
  │  - Caddy (TLS, Let's Enc)  │
  │  - WebSocket Reverse Proxy │
  └────────┬──────────────────┘
           │ 内部 VPC (SG: streamer → demo:8765)
  ┌────────▼──────────────────┐
  │ デモ EC2 c6i.xlarge Spot   │ Private Subnet
  │  - ai_server.py (Docker)   │
  └────────┬──────────────────┘
  ┌────────▼──────────────────┐
  │ 学習 EC2 c6a.4xlarge Spot  │ Private Subnet
  │  - SAC 訓練 (Docker)       │
  └────────┬──────────────────┘
           │
  ┌────────▼──────────────────┐
  │ VPC Endpoints (Private 用)  │
  │  - S3 (Gateway, 無料)        │
  │  - ECR API + DKR (Interface) │
  │  - CloudWatch Logs (I/F)     │
  └────────┬──────────────────┘
           ▼
       S3 / ECR / CloudWatch
```

- **配信のみ Public Subnet**、学習・デモは Private
- Private Subnet から AWS API へは Endpoint 経由（NAT Gateway 不採用）
- TLS は Caddy + Let's Encrypt（無料・自動更新）

### 8.5 セキュリティグループ

| SG | inbound | outbound |
|---|---|---|
| 配信 EC2 | 443/tcp from 0.0.0.0/0 (TLS) + 80 (ACME) | デモ EC2 8765, S3 |
| デモ EC2 | 8765 from 配信 SG | S3, ECR endpoint, Logs endpoint |
| 学習 EC2 | (なし) | S3, ECR endpoint, Logs endpoint |
| VPC Endpoint (vpce) SG | 443 from VPC CIDR | (なし) |

### 8.6 セッション間状態引き継ぎ

**セッション終了時 (Spot 中断 or 22:00 シャットダウン):**
- ブロック現ポーズを `s3://bucket/world_state/` に保存
- 最新モデル checkpoint は既に S3 (`models/`)

**セッション開始時:**
- S3 から world_state をロード → PyBullet に復元
- モデル取得して ai_server 起動
- 視聴者には「先週末の続き」として見える

### 8.7 Spot 中断対応

各 EC2 に systemd サービス `spot-handler.service` を常駐：
- IMDS の `/spot/instance-action` を 5 秒間隔でポーリング
- 中断検知 → S3 に状態保存 → Docker 停止 → 90 秒で終了

ASG + Mixed Instances Policy + capacity-optimized で自動再起動。

### 8.8 ECR / Docker

| Image | Dockerfile | ベース | 用途 |
|------|-----------|--------|------|
| `block-stacker/demo` | `Dockerfile` | python:3.11-slim | デモ EC2 (`mvp3.ai_server`) |
| `block-stacker/learner` | `Dockerfile.learner` | python:3.12-slim | 学習 EC2 (`mvp2.train`) |

両イメージとも **CPU torch wheel** を使用（GPU 不要）。配信 EC2 は Caddy をネイティブ実行（コンテナ化なし）。

### 8.9 監視

- **CloudWatch Logs**: 全 EC2 のアプリログ (Logs Interface Endpoint 経由)
- **CloudWatch Metrics**: CPU・メモリ・ネットワーク（標準）
- **アラート (CloudWatch Alarms → SNS)**:
  - Lambda エラー
  - Spot 中断率異常
  - CPU 高負荷継続

### 8.10 コスト試算

| 項目 | 単価 | 月額 |
|---|---|---|
| 学習 c6a.4xlarge Spot (16h) | $0.20/h × 16 | ¥480 |
| デモ c6i.xlarge Spot (176h) | $0.07/h × 176 | ¥1,848 |
| 配信 t4g.small Spot (176h) | $0.007/h × 176 | ¥185 |
| EBS gp3 180GB (稼働プロレート) | - | ¥314 |
| ECR Endpoint × 2 + Logs × 1 (24/7) | $7.3/月 × 3 | ¥3,300 |
| EIP アイドル (544h) | $0.005/h | ¥408 |
| Route 53 + S3 + CloudWatch | - | ¥330 |
| データ転送 (アウト) | - | ¥900 |
| **合計** | | **約 ¥7,765/月 (年 ¥93,000)** |

旧構成 (g4dn + Redis + 土日 68h 一括) ¥10,500/月 から **¥2,735 削減 + 視聴時間 2.6 倍**。

---

## 9. 決定事項サマリ

| カテゴリ | 項目 | 確定内容 |
|---|---|---|
| クライアント | 技術 | **Godot 4.4.1 .NET 版 + C#** |
| クライアント | 同時視聴 | 〜15 人 |
| クライアント | 描画 | MultiMeshInstance3D、`StandardMaterial3D` で色付け |
| クライアント | 三角柱描画 | 自前 ArrayMesh（サーバと頂点一致） |
| クライアント | 座標変換 | PyBullet Z-up → Godot Y-up、円柱のみ追加 +90° X 軸 |
| クライアント | 接続状態 UI | 「サーバとの通信を試行中...」中央表示（ドットアニメ） |
| クライアント | 地面描画 | 3m × 0.02m × 3m 灰色 BoxMesh |
| クライアント | グリッパー | なし |
| クライアント | UI | 接続状態のみ（他は 3D シーンのみ） |
| クライアント | カメラ | 固定（(1.5, 1.0, 1.5) → 原点） |
| 通信 | プロトコル | WebSocket 単一接続、type byte 判別 |
| 通信 | 同期方式 | スリープ/ウェイク明示モデル |
| 通信 | 送出レート | 60Hz（AWAKE blocks のみ） |
| 通信 | タイムスタンプ | サーバ単調時間 |
| サーバ | 構成 | 学習 EC2 / デモ EC2 / 配信 EC2 + S3 + VPC Endpoints |
| サーバ | モデル共有 | S3 経由 |
| サーバ | キャッシュ | **なし**（Redis 撤去、付録 D 復活条件参照） |
| AI | アルゴリズム | **SAC** (Stable-Baselines3) + 自動エントロピー |
| AI | **記憶構造** | **3 層: 勘 (NN) + 短期記憶 (観測内) + 重みつき長期記憶 (リプレイ)** |
| AI | event_type | 5 種類 (collapse/failure/height_record/success/no_progress) |
| AI | 観測 | Dict (blocks + heightmap + scalar + 短期記憶) |
| AI | 近接基準点 | タワー根本 |
| AI | 行動空間 | 7 次元連続 |
| AI | マニピュレーション | 階層化（上位=学習、下位=ソフト追従キャリア） |
| AI | カリキュラム | 5 Stage、**三角柱 → 円柱の順で投入（円柱が最難）** |
| AI | 卒業条件 | **①散布0で即卒業** または **②目標高さ到達の成功率 ≥ 60%（直近30回）**。目標 = 在庫満積み×0.6。env で上書き可 |
| AI | 降格 | なし |
| AI | Stage 情報 | クライアント非公開 |
| AI | 並列環境 | SubprocVecEnv n_envs=8（c6a.4xlarge 物理コア飽和） |
| ワールド | タワー定義 | 地面接続の**縦連結**成分のうち最高高度（横並びの塊は別扱い、斜面は縦扱い） |
| ワールド | 崩落判定 | H_high+H_low+タワー離散率+placing 除外 |
| 物理 | エンジン | PyBullet (Z-up) |
| 物理 | 形状 | **4 種: cube / cuboid / cylinder / triangular_prism** |
| 物理 | 三角柱実装 | GEOM_MESH 凸包メッシュ（自前頂点 6 個、面 8 個） |
| 物理 | 内部レート | 240Hz、ソルバー反復 100 |
| 物理 | 摩擦 | block-block 0.45 / block-ground 0.8 / block-wall 0.4 |
| 物理 | キャリア拘束 | point2point、max_force 8N、軌道速度 0.3m/s |
| AWS | リージョン | ap-northeast-1 (Tokyo) |
| AWS | 稼働 | 学習 隔週土曜 14-22 (16h/月) / デモ+配信 平日 14-22 (176h/月) |
| AWS | 学習 | **c6a.4xlarge Spot (AMD EPYC, CPU-only)** |
| AWS | デモ | c6i.xlarge Spot |
| AWS | 配信 | t4g.small Spot + Caddy（自動 TLS） |
| AWS | LB | なし（EC2 + EIP + Caddy） |
| AWS | スケジューラ | **EventBridge × 4 + Lambda 1 ペア（payload で対象 ASG 切替）** |
| AWS | Private Subnet 接続 | **S3 Gateway + ECR/Logs Interface Endpoint × 3** (NAT 不採用) |
| AWS | 状態引き継ぎ | S3 に world_state / models 保存 → 起動時復元 |
| AWS | Spot 中断対応 | IMDS 中断通知監視 → graceful save、ASG で自動再起動 |
| AWS | 監視 | CloudWatch Logs/Metrics/Alarms |
| AWS | **月額コスト** | **約 ¥7,765 (約 $51)** |
| 設定 | ファイル | world / physics / training / reward の 4 YAML |
| ローカル | 試運転 | tools/demo_checkpoints.ps1 で checkpoint 比較 |

---

## 関連ドキュメント

- [`docs/aws_deployment.md`](aws_deployment.md) — **デプロイ手順書**（AWS デプロイ手順 + 設計付録: コスト・Redis 復活条件・主要設計決定・未実装アイデア・ローカル開発）
- [`docs/local_demo.md`](local_demo.md) — **ローカル試運転手順書**（試運転 + checkpoint 比較ガイド）
- [`docs/log_reading.md`](log_reading.md) — **ログ解読マニュアル**（学習/推論ログの読み方）
- [`client/README.md`](../client/README.md) — Godot 4.4.1 .NET クライアントのセットアップ
