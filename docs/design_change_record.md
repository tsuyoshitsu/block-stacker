# 設計変更記録（過去仕様のアーカイブ）

**このファイルは「もう有効でない過去の仕様」を記録する場所**。現行仕様は
[`CLAUDE.md`](../CLAUDE.md) と [`block_stacker_design.md`](block_stacker_design.md) が基準。

用途:
- 「なぜこうなっているのか」を後から辿る
- 古い記事・スクリプト・会話ログに出てくる旧用語を現行仕様に読み替える
- docs 監査で「これは直し忘れか、それとも意図的な旧記述か」を判定する

> 各エントリの「変更」列はコミットハッシュ。`git show <hash>` で差分を確認できる。

---

## 1. 学習・チェックポイント

### 1.1 checkpoint の保存間隔: 等分割 → 絶対ステップ間隔

| | 旧仕様 | 現行 |
|---|---|---|
| 設定キー | `sac.checkpoint_splits: 5` | `sac.checkpoint_every: 50000` |
| 保存地点 | `total_timesteps` の 20/40/60/80/100% 地点（**5 本固定**） | 50,000 ステップ間隔（本数は run 長に依存） |
| save_freq 算出 | `total_timesteps // checkpoint_splits // n_envs` | `checkpoint_every // n_envs` |

**変更**: `a604878`（2026-07-13）
**理由**: `checkpoint_splits` 方式は `total_timesteps` に依存するため、`--target-stage` による
早期終了時に保存間隔が意図しない値になる。絶対ステップ間隔なら run 長に関係なく一定。

> 旧 `configs/training.yaml` のコメント: 「週次配信標準 (checkpoint_splits=5 で 800 刻み 5 本; 本格は 1M+ 推奨)」
> — `total_timesteps: 4000` で 800 ステップ刻み 5 本を生成し、週次配信の `step_01..05.zip` と 1 対 1 対応させる想定だった。

### 1.2 出力モデル数: 複数 checkpoint 前提 → 卒業プリセット 1 本が標準

| | 旧仕様 | 現行 |
|---|---|---|
| 想定出力 | 5 本（等分割）〜数本の checkpoint 列 | **通常は卒業プリセット 1 本** |
| 観察方法 | checkpoint を古い順に再生して「成長」を目視比較 | 1 本のモデルの到達度を確認 |

**変更**: `86fda5e`（docs 反映）
**理由**: `checkpoint_every: 50000` かつ `--target-stage 4` 既定のため、Stage 4 卒業が
50,000 ステップ未満で起きると**定期保存が一度も発火しない**。結果、卒業時の明示保存 1 本だけが残る。
定期保存の仕組み自体はコードに存在し、卒業まで 50k ステップを超える長い run では途中 checkpoint も生成される。

**これに伴い `local_demo.md` から撤去した記述**:
- 「発見された checkpoint (5 件)」形式の対話モード出力例
- Auto モード「20 個の checkpoint を各 30 秒ずつ…約 10 分かけて成長を一気に見られる」
- `local_loop.ps1` の「成長1巡再生」節
- timestep → 挙動の成長テーブル（5,000「赤ちゃんが触る」/ 25,000「2歳児」/ … / 500,000+「コツを掴んだ子供」）

> `tools/demo_checkpoints.ps1` / `tools/local_loop.ps1` は現存し、モデルが N 本あれば N 本とも扱える。
> 撤去したのは「複数本が前提」という**記述**であって、ツールの機能ではない。

### 1.2.1 ステージ進行: 卒業判定 → 固定ステップ制（**撤去**）

| | 旧仕様 | 現行 |
|---|---|---|
| 進行条件 | 卒業判定（下記 OR のどちらか） | **固定ステップ数**。`stages[].steps` 分走ったら次へ |
| 卒業① | `all_placed`（散布0）で**即卒業**（高さ条件なし） | **撤去**。指標として記録するのみ |
| 卒業② | `success_rate >= 0.6`（直近30エピソード） | **撤去**。指標として記録するのみ |
| 実装 | `GraduationCallback`（`_on_step` が False を返し `learn()` を停止） | `StageMonitorCallback`（記録のみ、決して止めない） |

**理由: 卒業①が誤検出していた。**

`find_tower_blocks` が返すのは「縦接触で連結された成分」であって「高く積まれた塔」ではない。
連結成分は**横に広がっても成立する**ため、`len(blocks) == len(tower)`（散布0）は
低く広い構造でも真になる。

実測（8個の cube、Stage 1 の目標高さは 0.240m）:

| 配置 | tower 判定 | 高さ | `all_placed` |
|---|---|---|---|
| 4ペアを独立に配置 | 2 | 0.100m | False |
| **4ペア＋上段を半個ずらし（レンガ積み）** | **8** | **0.100m** | **True**（誤） |
| 正当な8段タワー | 8 | 0.400m | True（正） |

上段が下段2個にまたがると縦エッジが連鎖し、全8個が1成分になる。高さは2段分しかないのに
「全部積み切った」と判定され、**高さ条件が無いため即卒業**していた。

実際の学習ログでも、全7回のステージ遷移すべてが `success_rate < 0.6`（多くは 0.000）の
まま発生しており、卒業②経由は一度も無かった。Stage 1→4 が 8,000〜12,000 ステップで
飛ぶ一方、success_rate は 0 のままという症状になっていた。

増幅要因（いずれも撤去済み）:
- `_ever_all_placed` がエピソード内で粘着 → 1フレームの誤判定が最後まで残る
- `done` を待たず毎ステップ判定し即 `learn()` 停止 → 1フレームでステージが飛ぶ

**現在は `all_placed` を「作品が1つにまとまった」ことを示す高さ非依存の指標として
`curriculum/all_placed_rate` / `all_placed_total` に記録する。進行には一切影響しない。**

#### ステージ予算の配分（U 字）

置き換え後の `stages[].steps` 既定値。合計 250,000（既定範囲 Stage 1-4 は 180,000）。

| Stage | steps | 比率 | 根拠 |
|---|---|---|---|
| 1 | 60,000 | 24% | ゼロから基礎獲得。以降の全ステージがこの方策を継承する |
| 2 | 35,000 | 14% | 同じ cube のまま個数と高さが増えるだけ。転移が最も効く |
| 3 | 40,000 | 16% | cuboid＝向きの概念 |
| 4 | 45,000 | 18% | 三角柱＝斜面 |
| 5 | 70,000 | 28% | 円柱＝転がる。既存方策が通用しない新規スキル |

単調増加ではなく**両端が重い U 字**にしてあるのは、Stage 1（ゼロから）と Stage 5（新規スキル）
だけが転移の恩恵を受けられないため。

総量は次の実測に基づく:
- スループット **約 2 steps/秒**（`time/fps` 中央値 2.0、2 run で一致）→ 180,000 steps ≈ 25 時間
- 学習シグナルは 1〜2 万ステップで出始める
- **ent_coef 暴走による発散が 17,000〜20,000 ステップで起きる**ため、現状それ以上の予算は検証不能

> 当初は 300k/300k/350k/400k/650k（合計 200万）を設定したが、実測 2 steps/秒では **11 日**かかる
> 非現実的な値だった。また「Stage 1 が最大の山」と説明しながら最小値（Stage 2 と同値）を
> 割り当てており、根拠と配分が矛盾していた。両方を修正したのが上表。

> **追記（同日、撤回）**: 一度 `checkpoint_every` を 50,000 → 10,000 に下げて
> 「成長のコマ送り用に 25 本出す」設定にしたが、これは §1.2 で決めた「1 run = モデル 1 本」
> という方針への逆行だった。§1.4 のとおり定期 checkpoint 自体を撤去した。

#### 実測による検証（2026-07-20、1/10 スケール 25,000 steps）

卒業判定の撤去後に 1/10 スケールで完走させた結果、**発散が消えた**。

| | SAC_1（旧・誤卒業あり） | SAC_3（修正後） |
|---|---|---|
| `critic_loss` 最大 | **2.17e15** | **5.11** |
| `ent_coef` 最大 | **6,315** | 0.98（初期値。暴走なし） |
| `success_rate` 最大 | 0.14 | **0.57** |
| `ep_rew_mean` | 記録なし | **-1.30 → +6.85** |

旧 run が反転した step 18,000 での直接比較でも `critic_loss` は 21.9 対 **1.73** だった。

**結論: ent_coef 暴走は独立した SAC ハイパラの問題ではなく、誤卒業の下流症状だった。**
実力ゼロのまま最難ステージに放り込まれ、報酬を得られないまま方策が潰れて log_prob が
跳ね上がったのが機序。卒業判定を撤去した時点で併せて解消した。**発散対策として別途
やるべきことはない。**

ステージ別の結果（この配分見直しの根拠）:

| Stage | success_rate | 所見 |
|---|---|---|
| 1 | 0.00 | 基礎習得期。報酬は改善継続 |
| 2 | 0.00 | 足踏み |
| 3 | **0.43** | 開花。報酬 +6.85 のピーク |
| 4 | **0.00** | **退行**。三角柱で崩れ、回復途中で予算切れ |
| 5 | 0.00 | 円柱で苦戦 |

> **難易度は目標高さでは測れない**。当初 Stage 2（目標 0.450m）が最難と分析したが誤り。
> Stage 3→4 は目標が 0.408m→0.420m とほぼ同じなのに success_rate が 0.43→0.00 に落ちた。
> 斜面（三角柱）・曲面（円柱）という**形状の難しさが支配的**。この結果を受けて
> Stage 3 を 40,000→25,000 に削り、Stage 4 を 45,000→60,000 に増やした。

---

## 1.2.2 散布0 の扱い: 放置 → 再配置してラウンド継続

| | 旧仕様 | 現行 |
|---|---|---|
| 学習側 | 何もしない（リセットなし） | `_rescatter_blocks()` で再配置し、ラウンドを継続 |
| デモ側 | `rescatter_blocks()` で再配置 | 同左（変更なし） |

**理由**: 学習側は散布0 の後、拾えるブロックが無いまま空振りが続いていた。
`time_penalty`（-0.05）を毎手払い続け、`max_actions_without_progress`（15）に達すると
`timeout_penalty`（-1.0）まで課され、合計 **-1.75** の減点とともに
**`event_type = failure` として記録**されていた。

`failure` の記憶初期重みは 0.7（崩落 1.0 に次ぐ2位）なので、**課題を完遂した経験が
強い負の記憶としてリプレイバッファに焼き付く**という逆転が起きていた。

再配置時は `steps_since_progress` を 0 に戻す（新しいラウンドなので空振り扱いにしない）。
`tower_best_height` と `_ever_all_placed` は据え置き（そのエピソードの達成として残す）。

## 1.2.3 指標の追加: 達成時のタワー高さ

`all_placed` は高さ非依存なので、**単体では「本物の塔」か「レンガ積み」か判別できない**。
そこで達成時のタワー高さを併記するようにした。

追加した指標:
- `curriculum/all_placed_height` — 散布0 達成時のタワー高さ（直近 window の平均）
- `curriculum/tower_height_mean` — エピソード最高到達高さの平均

Stage 1 なら 0.400m 付近なら本物の 8 段、0.100m 付近ならレンガ積みと判別できる。

> 関連する既知バグ: 以前「物理破綻で最難ステージが偽卒業」する不具合があり、
> `find_nearest_excluding` が None を返す経路には positive 確認が入っていた。
> だが通常経路（`env.py` の `len(blocks)==len(tower)`）は素通しで、そこが今回の原因。

### 1.3 学習の終了条件: 全ステージ完走 → `--target-stage` 卒業で打ち切り

| | 旧仕様 | 現行 |
|---|---|---|
| 既定挙動 | Stage 1→5 を全走、budget 打ち切りまで継続 | `--target-stage`（既定 **4**）卒業時点で終了しプリセット保存 |
| 全ステージ完走 | 既定 | `--target-stage 5` |
| budget 完走（旧挙動の再現） | 既定 | `--target-stage 9999` 等（到達しない値） |

**変更**: `a604878`（機能追加）/ `4abb5a4`（docs 反映）

> **さらにその後**: §1.2.1 の通り卒業判定自体を撤去したため、`--target-stage` は
> 「到達したら終了」ではなく **単に最後に走るステージの上限**（`--max-stage` と同義）に変わった。
> 既定値 4 は据え置きなので、既存のコマンドは同じステージ範囲で動く。

### 1.3.1 定期 checkpoint の撤去: 1 run = モデル 1 本

| | 旧仕様 | 現行 |
|---|---|---|
| 保存 | `CheckpointCallback` が `checkpoint_every` 間隔で定期保存＋最後にプリセット | **全ステージ走破後のプリセット 1 本のみ** |
| 設定 | `sac.checkpoint_every` | **撤去**（キー自体を削除） |
| 実装 | `CheckpointCallback` / `_compute_save_freq` | **撤去** |

**理由**: §1.2 で「1 run の出力はモデル 1 本」という前提に統一したのに、定期 checkpoint の
仕組みが残っていたため実際には複数本が `fresh/` に生成されていた（2026-07-20 の run では
9,996 / 19,992 / 25,002 の 3 本）。仕様と実態の不一致を、実装側を仕様に合わせて解消した。

> **トレードオフ**: 途中経過が残らないので、長時間 run がクラッシュすると成果を全部失う。
> 途中経過が欲しい場合は `--stage-steps` を刻んで複数回に分けて走らせる（run ごとに
> `fresh/` へ 1 本ずつ溜まり、`run_ts` が違うので衝突しない）。

これに伴い `tools/demo_checkpoints.ps1` / `local_loop.ps1` が扱う「複数 checkpoint」は、
1 run 内の途中経過ではなく **複数 run 分のモデル**を意味するようになった。

### 1.4 checkpoint ファイル名

| | 旧仕様 | 現行 |
|---|---|---|
| 命名 | `sac_<steps>_steps.zip` | `sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip` |
| 最終モデル | `sac_final.zip` | **廃止**。`fresh/` の最大ステップ checkpoint が最終モデル相当 |
| ソート基準 | ファイル名昇順 | `(run_ts, steps)` |

**変更**: `4257d88`（2026-06-27）
**理由**: run をまたいで checkpoint が `played/` に蓄積されるため、run を識別できないと
ファイル名が衝突し、ソート順も学習順と一致しなくなる。

### 1.5 `--resume` の撤去: 学習の再開機能を廃止

| | 旧仕様 | 現行 |
|---|---|---|
| train.py の再開 | `--resume` で前回 run の続きから学習 | **撤去**（毎 run ゼロから） |
| `_apply_resume()` / `_compute_elapsed_steps()` | train.py | 前者は削除、後者は **live_server へ移設** |
| `resume_state.json` / `replay_buffer.pkl` の保存 | 両方が読み書き | **残置**（読むのは live_server だけ） |

**理由**: 学習 run を毎回ゼロから走り切る運用に統一したため、train 側の再開機能が不要になった。
ただし **live_server のスナップショット引き継ぎは別機能**として残る（配信しながら継続学習し、
セッションをまたいで長期記憶を引き継ぐ）。`_compute_elapsed_steps`（時間減衰の算出）は
live_server が使うのでそちらへ移した。`configs.resume` セクションも live_server 専用として残置。

### 1.6 n_envs: 8 → 1（コンセプト判断）

| | 旧仕様 | 現行 |
|---|---|---|
| `sac.n_envs` | 8 | **1** |
| `sac.gradient_steps` | 8（n_envs と一致） | **1**（同上） |

**理由**: このサービスの目的は「優秀なモデルの生成」ではなく「子供が積み木で遊ぶのを眺める」体験で、
**学習はゆっくりでよく、不出来さを残したい**（CLAUDE.md 冒頭のコンセプト）。並列を増やすと速く
賢くなりすぎる。加えて:

- **n_envs は replay_buffer.pkl の形状に焼き込まれる**（`(buffer_size//n_envs, n_envs, ...)`）ため、
  一度決めると後から変えられない（学習と live_server で一致必須）。1 なら不一致事故が起きない。
- `gradient_steps` は n_envs と揃える必要がある。1 遷移あたりの勾配更新回数 = `gradient_steps / n_envs`
  で、これがズレると過学習・発散のリスク。両方 1 にして比率 1.0 を保つ。

**トレードオフ**: スループットは n_envs=6 の 3.6 steps/秒 → 1 では約 2.5 steps/秒（`gradient_steps`
削減で学習側が軽くなり、想定より速い）。速度より「ゆっくり育つ」コンセプト適合を優先。

### 1.7 プリセットの標準を「壁の手前」に（Stage 3 のみ・12,000 steps）

デフォルトの全ステージ学習とは別に、**ライブ配信用プリセットの標準レシピ**を
`--start-stage 3 --target-stage 3 --stage-steps 12000` に定めた。

**理由**: 2026-07-20 の Stage 3 のみ run（中断）で、**step 12,000〜15,000 に「不器用→習得」の
急激な壁**があると実測。

| step | ep_rew_mean | 高さ | success_rate |
|---|---|---|---|
| 11,506 | -0.64 | 0.086m | 0.00 |
| 14,687 | +6.33 | 0.436m | 0.63 |
| 21,794 | +12.4 | 0.607m | 0.97 |

30,000 まで走らせると success_rate 0.97 の「上手すぎる」モデルになりコンセプトに反する。
**12,000 で壁の手前に止める**と「掴む・運ぶはできるが積めない子供」になり、ライブ（Stage 5）で
未知形状に手こずる不出来さが残る。副産物として **Stage 3 ゼロ開始でも学習が立ち上がる**ことも
確認できた（掴む→運ぶ→積むを土台なしで獲得できる。事前の懸念は外れた）。

---

## 2. 観測・環境

### 2.1 観測形式: flat / dict 併存 → dict のみ

| | 旧仕様 | 現行 |
|---|---|---|
| パラメータ | `BlockStackerEnv(observation_format="flat" \| "dict")` | パラメータ自体を撤去（dict 固定） |
| flat 実装 | `pack_observation_flat()` / `observation_dim()` | 削除 |
| 型エイリアス | `ObservationFormat = Literal["flat", "dict"]` | 削除 |

**変更**: `52301ea`（2026-07-14）
**理由**: flat は 1 本の float32 ベクトルに全ブロックを詰める旧形式。Set Transformer + heightmap CNN の
`HybridFeatureExtractor` は Dict 観測（`blocks` / `blocks_mask` / `heightmap` / `tower_top_z`）前提で、
flat 経路は誰も使っていなかった。

### 2.2 カリキュラムの形状追加順

**旧**: cube → cuboid → **cylinder** → triangular_prism
**現行**: cube → cuboid → **triangular_prism** → cylinder

**理由**: 円柱は転がるため最も難しい。4 形状中の最後に投入するのが正しい難易度順。
（`block_stacker_design.md` に同旨の注記あり）

---

## 3. 報酬・物理

### 3.1 `flatness_bonus` / `flatness_scale` の撤去

`configs/reward.yaml` に `flatness_bonus: 0.0` / `flatness_scale: 0.1` が定義されていたが、
env の reward 計算に一度も配線されない **stub** だった。

**変更**: `5790f34`（コード）/ `0f11f14`（docs）

### 3.2 摩擦係数

| 項目 | 旧値 | 現行 | 理由 |
|---|---|---|---|
| `friction.block_to_ground` | 0.8 | 0.5 | `038e467` |
| `friction.block_to_block` | 0.6 | 0.45 | ブロック間の固着を緩和（×0.75） |

---

## 4. 配信・サーバ構成

### 4.1 本番配信: ai_server（推論専用） → live_server（配信＋学習融合）

| | 旧仕様 | 現行 |
|---|---|---|
| 本番デモ EC2 | `serving/ai_server.py`（推論のみ） | `serving/live_server.py`（配信しながらバックグラウンド学習） |
| 学習との関係 | 学習 EC2 と**完全分離**。S3 経由でモデルを受け渡し | 1 プロセス内で `train_model` / `serve_model` を並走、`WeightSyncer` で重み同期 |
| `ai_server` の位置づけ | 本番配信の主役 | **ローカル開発・動作確認用として存続** |

**変更**: live_server 実装は `6b769e4`（Step A）〜`ee7fa8b`（Step E）、docs 統一は `38dae83`
**詳細**: [`live_mode.md`](live_mode.md)

> 「学習 EC2 と配信 EC2 を分離し、S3 でモデルを受け渡す」という記述が残っていたら旧設計。

### 4.2 モデル S3 sync: 5 分毎 → ステップ間隔

**旧**: 学習側が **5 分毎**に `s3://bucket/models/` へ checkpoint sync
**現行**: `checkpoint_every`（既定 50000 steps）間隔＋卒業時に保存

**変更**: `0f11f14` / `38dae83`（docs）
**理由**: 保存契機が時間ベースからステップベースに変わったため（§1.1）。

### 4.3 `ShortTermMemory` の置き場所

**旧**: `serving/ai_server.py` 内に定義
**現行**: `serving/stm.py`

**変更**: `6b3b462`
**理由**: `live_server.py` からも使うため、推論サーバ実装から切り出した。

---

## 5. AWS 構成・運用

### 5.1 稼働スケジュール

**旧**: 全 ASG が**土日 14-22 に一括稼働**（68h/月）
**現行（暫定・調整中）**: 学習 = 隔週土曜 14-22（16h/月）/ デモ+配信 = 平日 14-22（176h/月、祝日除く）

**理由**: 学習頻度を絞り、配信時間を増やして視聴機会を 2.6 倍に。月額はほぼ据置。

> **現行の値も確定値ではない**。docs 上は「暫定・調整中」と明示している（`38dae83`）。

### 5.2 学習インスタンス: GPU → CPU

**旧**: `g4dn`（GPU）
**現行**: `c6a.4xlarge`（AMD EPYC, CPU-only）

**理由**: NN が小規模で PyBullet が CPU bound。GPU が活かせていなかった。月 ¥3,600 → ¥480。

### 5.3 ElastiCache Redis: 撤去

実装上 import が無く未使用だったため撤去（月 ¥2,460 節約）。
**再導入条件**は [`aws_deployment.md`](aws_deployment.md) 付録 D に記載。

### 5.4 IaC: Terraform → AWS CLI + PowerShell

**現行**: `deploy/` 配下の PowerShell スクリプト。
旧 Terraform 版は `infra-terraform/` に**参照用として保持**（メンテはしていない）。

---

## 6. パッケージ名・パス

| | 旧 | 現行 | 変更 |
|---|---|---|---|
| パッケージ | `mvp2` / `mvp3` | `training` / `serving` | `aaaf5f3` |
| 実行時データパス | `output/mvp2/` | `output/training/` | `1954943` |
| checkpoint ディレクトリ | `output/training/checkpoints/` | `fresh/` + `played/` | — |
| マイルストーン表記 | コード・設定内の「MVP 0〜3」ラベル | 撤去 | `286fa1d` / `81959ff` |

> `output/training/checkpoints/` が残っていても自動的には使われない（旧ディレクトリ）。

---

## 6.5 計測の欠落: 学習 env が Monitor で包まれていなかった

**旧**: `train.py` の `_make_env` が `BlockStackerEnv` をそのまま返していた
**現行**: `Monitor(env)` で包む

**理由**: SB3 は Monitor が info に載せる `"episode"` キーから `rollout/ep_rew_mean` /
`ep_len_mean` を算出する。Monitor が無いと**報酬曲線が TensorBoard に一切出ない**。
`rollout/success_rate` は `info["is_success"]` から独立に出るため、
「グラフはあるのに報酬だけ無い」状態になり欠落に気づきにくかった。

実際、卒業誤検出を調査した時点の run には報酬系のタグが1つも無く、
スカラーは 10 種（`curriculum/*`, `rollout/success_rate`, `time/fps`, `train/*`）のみだった。

---

## 7. 開発環境

### 7.1 `uv sync 厳禁` の適用範囲

`.venv` は **python.org 製 CPython 3.12** で作成されており、uv 管理ではない。
`uv sync` で作り直すと `No Python at '...'` で全コマンドが落ちる。

**ただしこの制約はローカルデモ実行 / `.venv` での開発時に限る**。
Lambda zip ビルド等の別環境での uv 使用は対象外（`aws_deployment.md` §1.1 の前提条件は有効）。

**明確化**: `38dae83`

> 経緯: uv 管理 python → 対話シェルから到達不能 → Anaconda で作り直し → 古い VC ランタイムで
> torch の `c10.dll` 初期化失敗（`WinError 1114`）→ 素の python.org 3.12 で解決。
> `pybullet` は numpy2 対応のソースビルド版が必須（PyPI wheel は numpy1 ABI で壊れる）。

---

## 関連

- 現行仕様の基準: [`../CLAUDE.md`](../CLAUDE.md)
- 設計書: [`block_stacker_design.md`](block_stacker_design.md)
- ライブ配信モード: [`live_mode.md`](live_mode.md)
- ローカル試運転: [`local_demo.md`](local_demo.md)
- AWS デプロイ: [`aws_deployment.md`](aws_deployment.md)
