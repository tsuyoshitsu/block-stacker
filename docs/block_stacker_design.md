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
│ - 月初 Stage3 のみ 5k steps           │
│ - 完成後プリセットを S3 へ保存        │
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
│ live EC2 (c6a.2xlarge, AMD 4コア)     │
│ - live_server.py (配信+バックグラウンド学習融合) │
│ - 物理1x速、フルアニメーション         │
│ - WebSocket :8765 で配信             │
└──────────────┬──────────────────────┘
               │ 内部 VPC (SG 制限)
               ▼
┌─────────────────────────────────────┐
│ 配信 EC2 (t4g.small ARM, Caddy)      │
│ - 自動 TLS (Let's Encrypt)           │
│ - reverse_proxy wss://→ live:8765   │
└──────────────┬──────────────────────┘
               │ WebSocket (wss)
               ▼
        Godot 4.4 .NET クライアント (C#) × ~15
```

### サーバ構成

| コンポーネント | 役割 |
|---|---|
| 学習 EC2 (c6a.4xlarge Spot) | 月初にプリセット生成（Stage3 のみ 5k, n_envs=1）→ S3。※5k/n_envs=1 には過剰、§8.2 参照 |
| live EC2 (c6a.2xlarge Spot) | 1x速で物理シム実行＋バックグラウンド学習、WebSocket 配信。選定は §8.3.1 |
| 配信 EC2 (t4g.small Spot, ARM) | Caddy 自動 TLS + リバプロでクライアントへブロードキャスト |
| S3 | モデル重み + 持続ワールド状態 + configs |
| VPC Endpoints | S3 Gateway (無料) + ECR Interface × 2 + Logs Interface (Private Subnet 用) |

### モデル共有フロー

1. 学習側（月初）: Stage3 のみ 5k steps のプリセット 1 本を `s3://bucket/models/` へ sync
2. live 側: 起動時に S3 から最新モデルを取り込み、live_server で配信（バックグラウンド学習込み）

> **設計変更履歴**:
> - ElastiCache Redis は未使用のため撤去（実装上 import 無し、月 ¥2,460 節約）
> - 学習を GPU (g4dn) → CPU (c6a) に変更（NN が小さく PyBullet が CPU bound なため）
>
> 過去仕様の一覧は [`design_change_record.md`](design_change_record.md) にまとめてある。

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
- 並列環境 n_envs=1（ゆっくり育てるコンセプト。速度より長期連続稼働を優先）

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

#### 学習状態の引き継ぎ（live_server のスナップショット）

**train.py の `--resume` は廃止した**（学習 run は毎回ゼロから走り切る前提）。
引き継ぎが残るのは live_server のスナップショット機構のみ:

| 引き継ぐもの | 方法 | 減衰 |
|------|------|------|
| **勘（NN重み）** | `find_latest_checkpoint` で最新 run のモデルを `SAC.load()` | そのまま（無加工） |
| **長期記憶（リプレイバッファ）** | `replay_buffer.pkl` から復元 | 経過日数 × `steps_per_day` だけ `global_step` を加算し、全記憶の重みを一律減衰 |

短期記憶（recent_* deque）はセッション境界でクリアされる（設計通り）。

**減衰の仕組み（Method A）**: `replay_buffer.global_step += elapsed_steps` で
全スロットの `age = global_step - birth_steps` が同時に増加し、現在重み = `initial_w × decay_rate^age`
が一斉に古くなる。強い記憶（`collapse` 初期重み 1.0）は弱い記憶（`no_progress` 0.1）
より長く生き残る（`weight_floor` が下限）。

train.py は学習完了時に `replay_buffer.pkl` と `resume_state.json` を `output/training/` に保存する。
live_server が次回起動時にこれを読み、`timestamp` から経過日数を算出して減衰を適用する。

設定（`configs/training.yaml` の `resume:` セクション。**読むのは live_server のみ**）:

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

#### Stage 進行（固定ステップ制。**卒業判定は無い**）

各ステージは決められたステップ数だけ走り、**成績によらず**次のステージへ進む。

```yaml
stages:
  - id: 1
    steps: 60000              # このステージで走る env ステップ数
    ...
graduation:
  window: 30                  # 指標（success_rate / all_placed_rate）の移動平均幅
  threshold: 0.6              # 未使用（卒業判定の撤去に伴い残置のみ）
  ratio: 0.6                  # 目標高さ = 在庫満積み高さ × ratio（is_success の判定に使う）
demotion_enabled: false       # Stage ダウンなし、一方向のみ進行
```

予算は CLI で上書きできる。`--stage-steps 100000`（全ステージ一括）、
`--stage-steps 60000,35000,40000`（実行ステージへ順に割当。要素数不一致はエラー）。

`window` / `ratio` は**コンテナ環境変数**でも上書きできる（優先順位:
env var > training.yaml > 既定）。本番（Docker/ECS）で `-e BS_GRADUATION_RATIO=0.7` 等。
詳細は[`docs/aws_deployment.md`](aws_deployment.md) のデプロイ節。

#### 指標（進行には影響しない記録専用）

`StageMonitorCallback` が TensorBoard に出す。**次回のステップ数配分を決める材料**。

```
is_success = tower_best_height >= 目標高さ   # 高さ指標  → curriculum/success_rate
all_placed = 全ブロックが1つの連結成分        # 高さ非依存 → curriculum/all_placed_rate
                                              #             curriculum/all_placed_total
目標高さ   = 在庫満積み高さ × ratio
```

> **`all_placed` は「高く積めた」ではなく「作品が1つにまとまった」を意味する。**
> `find_tower_blocks` が返すのは「縦接触で連結された成分」であり、連結成分は横に広がっても
> 成立する。実測では 8 個をレンガ積みにした高さ 0.100m の構造（目標 0.240m）でも
> `len(blocks) == len(tower)` が真になる。
>
> **かつてこれを「散布0＝積み切った」とみなして即卒業のトリガーにしていたため、
> success_rate が 0 のまま Stage 1→4 が数千ステップで飛ぶ不具合が起きていた。**
> 高さ条件が無いこと自体が原因なので、再びこれを進行条件に使わないこと。
> 経緯と実測値は [`docs/design_change_record.md`](design_change_record.md) §1.2.1。

`stable_duration` の継続判定は未実装。

#### エピソードタイムアウト

- 最大ステップ数: 30
- 高さが更新されない行動が連続 15 回でタイムアウト（failure 扱い）

#### 実装状況（オートカリキュラム）

**実装済み**（[`training/train.py`](src/block_stacker/training/train.py) + [`training/curriculum.py`](src/block_stacker/training/curriculum.py)）。

- **既定で** Stage 1→N を自動進行（`--no-curriculum` で Stage 1 のみに切替）。
- **進行は固定ステップ制**。各ステージは `stages[].steps`（CLI `--stage-steps` で上書き可）だけ走り、
  成績によらず次へ進む。`StageMonitorCallback` は指標を記録するだけで `learn()` を止めない。
  既定配分は 60k / 35k / 5k / 60k / 70k（Stage 1〜5、合計 23万。Stage 1-4 は 16万）。
  Stage 1（ゼロから）と Stage 4/5（斜面・曲面）が重い。**Stage 3=5k はプリセット生成の標準値**を
  兼ねる（壁の手前で止める。全 run で回すなら Stage 3 が短すぎるので --stage-steps 上書き前提）。
  総量は n_envs=1 実測 約2.5 steps/秒（16万 ≈ 18時間）を踏まえた値。
- 観測空間は全ステージ共通（`max_blocks=8` 等で固定）なので、**同じ NN・記憶バッファを
  `model.set_env()` で引き継いだまま** env だけ差し替える。タイムステップ計数・TensorBoard も連続。
  SB3 は `reset_num_timesteps=False` のとき `total_timesteps` に `num_timesteps` を足すため、
  `learn()` にはステージ予算をそのまま渡せばその分だけ進む。
- 学習 env は **`Monitor` で包む**。これが無いと `rollout/ep_rew_mean` が出ず報酬曲線が消える。
- 保存: **全ステージ走り切った時点のプリセット 1 本のみ**（`fresh/sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip`）。
  定期 checkpoint は撤去した（`sac_final.zip` も廃止）。
- `--total-timesteps` は**全体の安全上限**。無指定なら `sac.total_timesteps`（既定 null）→
  ステージ予算の合計をそのまま使う。上限を超える分は後半ステージが切り詰められる。
- `--start-stage`（既定 **3**）/ `--target-stage`（既定 **3**）/ `--max-stage` で走る範囲を決める。
  **既定は「Stage 3 のみ」＝引数なし実行がそのままプリセット生成**になる。
  フルカリキュラムは `--start-stage 1 --target-stage 4` を明示（既定では走らない）。
  卒業判定の撤去に伴い「到達したら終了」ではなく単なる範囲指定。
- **本番配信（[`serving/live_server.py`](src/block_stacker/serving/live_server.py)）**:
  学習（train_model）と配信（serve_model）を 1 プロセスに融合した形式。常に最終ステージ（Stage 5）で配信。
  既定モデルは `find_latest_checkpoint`（ソートキー `(run_ts, steps)` 降順の最大値）で自動選択。
  [`serving/ai_server.py`](src/block_stacker/serving/ai_server.py)（推論専用・学習なし）はローカル開発・動作確認用として残る。
  常に最終ステージ（Stage 5）で配信しながら、バックグラウンドスレッドで SAC を継続訓練する。

  ```
  .venv\Scripts\python.exe -m block_stacker.serving.live_server \
      --snapshot-dir output/training --duration 28800 --n-envs 4
  ```

  | コンポーネント | スレッド | 役割 |
  |---|---|---|
  | `serve_model` | asyncio | 現在の最良重みで `predict()` → WebSocket 配信 |
  | `train_model` | daemon スレッド | `SAC.learn()` でバックグラウンド訓練 |
  | `WeightSyncer` | スレッド境界 | Lock + `.clone()` で `--sync-every` 毎に重みをコピー |
  | `LiveCallback` | daemon スレッド | `stop_event` 監視 + 重みプッシュ |

  **セッションライフサイクル（既定 8h）**:
  1. `--snapshot-dir` から NN 重み + replay_buffer.pkl + resume_state.json を復元。
  2. replay_buffer には経過日数 × steps_per_day 分の時間減衰を適用（`_apply_live_resume`）。
  3. 配信と訓練を並走。`--sync-every` ステップごとに train_model → serve_model へ重みをコピー。
  4. `--duration` 経過後: `stop_event` → training thread join → `_save_live_snapshot()` → 終了。

  `_save_live_snapshot()` は `fresh/sac_<run_ts>_<steps>_steps.zip` + `replay_buffer.pkl` + `resume_state.json` を書き出す。次回起動時に `--no-resume` なしで自動的に引き継がれる。

  **スレッド安全性**: `WeightSyncer.push()` は training thread 側でロックを取ってポインタを書き換えるだけ（ns 単位）。`pull()` は 240Hz の physics loop 内でロック取得 → ポインタ swap → ロック解放 → `load_state_dict()`（ロック外）。
  物理ループが学習スレッドのロック待ちでブロックされる時間は実質ゼロ。

  **運用手順（プリセット生成 / n_envs 最適化 / Spot 中断対策）は [`docs/live_mode.md`](live_mode.md) を参照。**

  **PyBullet スレッド安全性**: training 用 `SubprocVecEnv` は独立プロセスで PyBullet を保持するため、配信用の asyncio PyBullet（メインスレッド）とは完全に分離される。

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
| 摩擦 (block-ground) | 0.5 | 地面摩擦を下げて滑りやすく（0.8→0.5） |
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
  # 注: 卒業判定は撤去済み。進行は stages[].steps の固定ステップ制で決まる。
  graduation:
    rule: "success_rate"
    window: 30        # 指標の移動平均幅。env BS_GRADUATION_WINDOW で上書き可
    threshold: 0.6    # **未使用**（卒業判定撤去のため残置のみ）
    ratio: 0.6        # 目標高さ=在庫満積み×ratio。env BS_GRADUATION_RATIO で上書き可
  demotion_enabled: false
  # 目標高さは ratio から動的算出するので stage に target_height は持たない。
  stages:
    - id: 1
      name: "Stage 1: cube only, low target"
      shapes_allowed: [cube]
      inventory: {cube: 8}
      steps: 60000                # ← このステージを走らせる env ステップ数（進行を決める）
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
  total_timesteps: null         # 全体の安全上限。null ならステージ予算の合計をそのまま使う
  n_envs: 1                     # ゆっくり育てるコンセプト。gradient_steps と必ず揃える
  buffer_size: 50000            # 実測 replay_buffer.pkl ≈ 1.7GB（heightmap が支配的）
  learning_starts: 200
  batch_size: 256
  learning_rate: 0.0003
  tau: 0.005
  gamma: 0.99
  train_freq: 1
  gradient_steps: 1             # n_envs と揃える（ズレると1遷移あたり更新回数が変わり発散リスク）
  ent_coef: "auto"
  target_update_interval: 1
  log_interval: 4
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

**運用モデル（暫定）**: 学習は「月初のシード生成」＋「平日の連続学習」の2段構え。

- **① プリセット生成（月初・月1回）**: 月の初日に **Stage 3 のみ・5,000 steps** の短時間学習で
  その月のシードモデルを作る（n_envs=1、実測 約2.5 steps/秒 → **約1時間**）。
  レシピは**引数なし実行**（既定が start=target=3 / Stage3 steps=5,000）。
- **② 学習配信（平日・日中8時間）**: live EC2 で `live_server` を回し、**配信しながら
  バックグラウンドで継続学習**する。前営業日のスナップショットを引き継ぐので、月を通して
  シードから少しずつ賢くなる。

| Scheduler | Cron (UTC) | JST 時刻 | 月間時間 | 対象インスタンス |
|----------|---------|---------|---------|---------|
| **bs-learner-start** | `cron(0 0 1 * ? *)` | 毎月 1 日 09:00 | プリセット生成 ~1h/月 | bs-learner |
| **bs-learner-stop** | `cron(0 2 1 * ? *)` | 毎月 1 日 11:00 | 同上（完了で self-stop、この cron は保険） | 同上 |
| **bs-live-start** | `cron(0 1 ? * MON-FRI *)` | 月-金 10:00 | live+配信 176h/月 | bs-live + bs-streamer |
| **bs-live-stop** | `cron(0 9 ? * MON-FRI *)` | 月-金 18:00 | 同上 | 同上 |

> **スケジュール（暫定）**: 上記の稼働時間帯・学習頻度・ステップ数はいずれも**暫定値**で確定していない。
> 特に「月初・5,000 steps」「平日 10-18 の 8h」は運用しながら調整する前提。
>
> **プリセット生成インスタンスは過剰の可能性**: 5,000 steps を n_envs=1 で回すのは 1 コア×約35分で、
> 学習 EC2 の c6a.4xlarge（16 vCPU）は明らかにオーバースペック。将来は小型インスタンスへ寄せるか、
> live EC2 の月初プリステップとして畳み込む案がある（未決）。
>
> 設計変更履歴: 旧版は隔週土曜にフルカリキュラム学習（16h/月）していたが、n_envs=1 の
> ゆっくり育てる方針＋ live_server 融合（配信中に継続学習）へ移行したことで、
> 専用学習は「月初のシード生成」だけに縮小した。

#### Lambda 構成

1 ペア (`bs-scale-up` / `bs-scale-down`) を共有し、各 Scheduler の `input` payload で対象インスタンスを指定：

```json
{ "instance_ids": ["<learner-id>"] }
{ "instance_ids": ["<live-id>", "<streamer-id>"] }
```

handler.py の `_resolve_instance_ids(event)` が payload 優先、未指定なら env var `INSTANCE_IDS` フォールバック。
**ASG は撤去済み**で、Lambda は `ec2:StartInstances` / `StopInstances` を呼ぶ（経緯は design_change_record.md）。

祝日は `jpholiday.is_holiday()` で skip（学習・live 両方とも）。

### 8.3 インスタンス構成

| 役割 | インスタンス | 購入方式 | スペック |
|---|---|---|---|
| 学習（専用バッチ） | c6a.4xlarge | **Spot** | 16 vCPU (8 物理コア) / 32GB / AMD EPYC CPU-only |
| live（配信＋学習融合） | **c6a.2xlarge（推奨）** | **Spot** | 8 vCPU (4 物理コア) / 16GB / AMD EPYC。3層の選定は §8.3.1 |
| 配信 | t4g.small (ARM) | **Spot** | 2 vCPU / 2GB + Caddy |

> 設計変更履歴:
> - 学習を GPU (g4dn) → CPU (c6a) に変更。NN が小規模で PyBullet が CPU bound なため GPU が活かせていなかった。月 ¥3,600 → ¥480 に削減。
> - live を c6i.xlarge（2 物理コア）→ c6a.2xlarge に再選定。live_server 融合（配信＋バックグラウンド学習）は
>   表示の 240Hz 物理に1コア専有したいので 2 コアでは足りず、フレーム落ちが出るため（§8.3.1）。

#### 8.3.1 live（配信＋学習融合）インスタンスの選定

**選定軸（描画は視聴者のクライアントで行うので、サーバ負荷は物理＋配信＋学習のみ）**:

1. **CPU＝単一コア律速**。表示の 240Hz `world.step()` は PyBullet の単一スレッドで、1歩 4.17ms を
   守るには**物理コアを1つ専有**したい。効くのは物理コア数（表示に1つ割けるか）と単一コアの実効クロック。
   コア数を増やしても1歩は速くならない。
2. **メモリ＝replay_buffer が主因**。`buffer_size=50000` で観測（heightmap 16KB/件が支配的）を
   obs+next_obs 保持するため **約 1.7GB**。実働は +torch/python/PyBullet×2/OS で **約 4GB**。
   長期記憶（`buffer_size`）を増やすと**線形に増える**（10万→3.4GB、20万→6.8GB）。
3. **ネットワークは非制約**。60Hz スナップショット × 〜15 視聴者 ≈ 数 Mbps。選定要因にならない。
4. **コアを増やしすぎない**。コンセプトが「ゆっくり育つ子供」で学習並列（n_envs）を上げて
   速く賢くする必要がないため、増強の主眼は**表示の滑らかさ（clock）と記憶容量（RAM）**であって
   コア数ではない。

| 段階 | インスタンス | 物理コア / RAM | 世代 | 位置づけ | Spot 目安 (176h/月) |
|---|---|---|---|---|---|
| **最低** | c6a.xlarge | 2 / 8GB | Milan | 最安。表示と学習が物理コアを共有し、重い物理時（settle 中）にフレーム落ち。現行 c6i.xlarge と同じ 2 コア級の限界。`--n-envs 0` 併用や多少のジッタ許容が前提 | ~$14 / ¥2,100 |
| **推奨** | **c6a.2xlarge** | **4 / 16GB** | Milan | 表示に1コア専有＋学習に余裕。RAM は実働の4倍で長期記憶を 10〜15万件まで伸ばせる。コスパ最良 | ~$23 / ¥3,450 |
| **最高** | m7a.2xlarge | 4 / 32GB | Genoa | 最新世代で単一コア clock 最高＝**表示が最も滑らか**。RAM 32GB で長期記憶を最大化（20万件〜）。コア数は据え置き（学習並列は不要） | ~$30 / ¥4,500 |

> Spot 価格は変動するため目安。参考: 旧 c6i.xlarge は 2 物理コア / 8GB / ~$12.3・¥1,848。
> 「最高」で敢えてコア数を増やさない（4xlarge にしない）のは、slow-learning コンセプトでは
> 学習スループットを上げる価値が薄く、上げるべきは表示品質と記憶容量だから。

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
           │ 内部 VPC (SG: streamer → live:8765)
  ┌────────▼──────────────────┐
  │ live EC2 c6a.2xlarge Spot  │ Private Subnet
  │  - live_server.py (Docker) │
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

- **配信のみ Public Subnet**、学習・live は Private
- Private Subnet から AWS API へは Endpoint 経由（NAT Gateway 不採用）
- TLS は Caddy + Let's Encrypt（無料・自動更新）

### 8.5 セキュリティグループ

| SG | inbound | outbound |
|---|---|---|
| 配信 EC2 | 443/tcp from 0.0.0.0/0 (TLS) + 80 (ACME) | live EC2 8765, S3 |
| live EC2 | 8765 from 配信 SG | S3, ECR endpoint, Logs endpoint |
| 学習 EC2 | (なし) | S3, ECR endpoint, Logs endpoint |
| VPC Endpoint (vpce) SG | 443 from VPC CIDR | (なし) |

### 8.6 セッション間状態引き継ぎ

引き継ぎは2つの周期で起きる:
- **日次（平日ごと）**: live_server が終了時にスナップショット（NN 重み＋長期記憶＋world_state）を
  S3 に保存し、翌営業日に復元する。**前営業日の続き**から連続学習・連続配信になる。
- **月次（月初）**: プリセット生成が新しいシードモデルを作り、その月の起点にする。
  長期記憶は経過日数ぶんの時間減衰を受けて引き継がれる（`resume:` 設定）。

**セッション終了時 (Spot 中断 or 18:00 シャットダウン):**
- ブロック現ポーズを `s3://bucket/world_state/` に保存
- 最新モデル + replay_buffer を S3 (`models/` / `state/`) に保存（live_server の `_save_live_snapshot`）

**セッション開始時:**
- S3 から world_state をロード → PyBullet に復元
- モデル + 長期記憶を取得して live_server 起動（前営業日の続き）
- 視聴者には「昨日の続き」として見える

### 8.7 Spot 中断対応

各 EC2 に systemd サービス `spot-handler.service` を常駐：
- IMDS の `/spot/instance-action` を 5 秒間隔でポーリング
- 中断検知 → S3 に状態保存 → Docker 停止 → 90 秒で終了

**自動再起動は無い（ASG 撤去に伴い手放した）**。Spot 中断後は手動で `start-instances` する。
在庫切れで起動できない場合も手動でインスタンスタイプを変更して再作成する（docs/aws_deployment.md §8）。
stop は terminate と違い EBS を保持するので、スナップショットや world_state はディスク上に残る。

### 8.8 ECR / Docker

| Image | Dockerfile | ベース | 用途 |
|------|-----------|--------|------|
| `block-stacker/live` | `Dockerfile` | python:3.11-slim | live EC2 (`serving.live_server`) |
| `block-stacker/learner` | `Dockerfile.learner` | python:3.12-slim | 学習 EC2 (`training.train`) |

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
| プリセット生成 c6a.4xlarge Spot (~1h/月) | $0.20/h × 1 | ¥30 |
| live（配信＋学習） c6a.2xlarge Spot (176h) | ~$0.13/h × 176 | ¥3,435 |
| 配信 t4g.small Spot (176h) | $0.007/h × 176 | ¥185 |
| EBS gp3 180GB (稼働プロレート) | - | ¥314 |
| ECR Endpoint × 2 + Logs × 1 (24/7) | $7.3/月 × 3 | ¥3,300 |
| EIP アイドル (544h) | $0.005/h | ¥408 |
| Route 53 + S3 + CloudWatch | - | ¥330 |
| データ転送 (アウト) | - | ¥900 |
| **合計** | | **約 ¥8,900/月 (年 ¥107,000)** |

> live は**推奨 c6a.2xlarge**前提（§8.3.1。最低 c6a.xlarge なら約 ¥7,600、最高 m7a.2xlarge なら約 ¥9,900）。
> プリセット生成を月初 5k steps（~35分）に縮小したことで学習費は ¥480→¥30 に。
> 稼働時間・頻度・インスタンス段階はいずれも暫定で、確定後に再計算する。

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
| サーバ | 構成 | 学習 EC2 / live EC2 / 配信 EC2 + S3 + VPC Endpoints |
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
| AI | ステージ進行 | **固定ステップ制**（卒業判定なし）。予算 60k/35k/5k/60k/70k。**train 既定は Stage 3 のみ＝プリセット生成**、`--stage-steps` で上書き可 |
| AI | 降格 | なし |
| AI | Stage 情報 | クライアント非公開 |
| AI | 並列環境 | **n_envs=1**（ゆっくり育てるコンセプト。gradient_steps も 1 と揃える） |
| ワールド | タワー定義 | 地面接続の**縦連結**成分のうち最高高度（横並びの塊は別扱い、斜面は縦扱い） |
| ワールド | 崩落判定 | H_high+H_low+タワー離散率+placing 除外 |
| 物理 | エンジン | PyBullet (Z-up) |
| 物理 | 形状 | **4 種: cube / cuboid / cylinder / triangular_prism** |
| 物理 | 三角柱実装 | GEOM_MESH 凸包メッシュ（自前頂点 6 個、面 8 個） |
| 物理 | 内部レート | 240Hz、ソルバー反復 100 |
| 物理 | 摩擦 | block-block 0.45 / block-ground 0.5 / block-wall 0.4 |
| 物理 | キャリア拘束 | point2point、max_force 8N、軌道速度 0.3m/s |
| AWS | リージョン | ap-northeast-1 (Tokyo) |
| AWS | 稼働（暫定） | プリセット生成 月初1日 09:00・5k steps (~35分/月) / live（配信＋学習） 平日 10-18 (176h/月) |
| AWS | 学習 | **c6a.4xlarge Spot (AMD EPYC, CPU-only)** |
| AWS | live | c6a.2xlarge Spot（推奨。最低 c6a.xlarge / 最高 m7a.2xlarge、§8.3.1）|
| AWS | 配信 | t4g.small Spot + Caddy（自動 TLS） |
| AWS | LB | なし（EC2 + EIP + Caddy） |
| AWS | スケジューラ | **EventBridge × 4 + Lambda 1 ペア（payload で対象インスタンス切替、start/stop）** |
| AWS | Private Subnet 接続 | **S3 Gateway + ECR/Logs Interface Endpoint × 3** (NAT 不採用) |
| AWS | 状態引き継ぎ | S3 に world_state / models 保存 → 起動時復元 |
| AWS | Spot 中断対応 | IMDS 中断通知監視 → graceful save。**自動再起動なし**（ASG 撤去。手動 start） |
| AWS | 監視 | CloudWatch Logs/Metrics/Alarms |
| AWS | **月額コスト（暫定）** | **約 ¥8,900 (約 $59)**（推奨 c6a.2xlarge 前提。§8.10） |
| 設定 | ファイル | world / physics / training / reward の 4 YAML |
| ローカル | 試運転 | tools/demo_checkpoints.ps1 で checkpoint 比較 |

---

## 関連ドキュメント

- [`docs/aws_deployment.md`](aws_deployment.md) — **デプロイ手順書**（AWS デプロイ手順 + 設計付録: コスト・Redis 復活条件・主要設計決定・未実装アイデア・ローカル開発）
- [`docs/local_demo.md`](local_demo.md) — **ローカル試運転手順書**（試運転 + checkpoint 比較ガイド）
- [`docs/log_reading.md`](log_reading.md) — **ログ解読マニュアル**（学習/推論ログの読み方）
- [`client/README.md`](../client/README.md) — Godot 4.4.1 .NET クライアントのセットアップ
