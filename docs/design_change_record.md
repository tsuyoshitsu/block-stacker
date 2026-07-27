# 設計変更記録（過去仕様のアーカイブ）

**このファイルは「もう有効でない過去の仕様」を記録する場所**。現行仕様は
[`CLAUDE.md`](../CLAUDE.md) と [`block_stacker_design.md`](block_stacker_design.md) が基準。

用途:
- 「なぜこうなっているのか」を後から辿る
- 古い記事・スクリプト・会話ログに出てくる旧用語を現行仕様に読み替える
- docs 監査で「これは直し忘れか、それとも意図的な旧記述か」を判定する

> 各エントリの「変更」列はコミットハッシュ。`git show <hash>` で差分を確認できる。

---

## 0. 現行設計スナップショット（2026-07-21 時点）

このファイルの本体は「旧仕様」なので、**読み違え防止のため現行の到達点をここに要約**しておく。
以降の §1〜§7 に出てくる仕様は、断りがない限り**すでに置き換わったもの**である。

### 学習と配信

- **live_server が本番**。1プロセス内で **2つの独立した PyBullet 世界**を並走させる
  （asyncio=表示 240Hz / バックグラウンド=`SAC.learn()`）。`WeightSyncer` が `--sync-every`
  ごとに学習側の重みを表示側へコピーする。**表示は学習ロールアウトそのものではない**
- `ai_server` はローカル開発・動作確認用として残置
- **既定 `--n-envs 1` / `--sync-every 50`**（運用値と一致。n_envs は config の `sac.n_envs` と揃える）

### ステージ進行

- **卒業判定は存在しない**。各ステージは `stages[].steps` を消化したら**成績によらず**次へ
- `--start-stage`（既定 **3**）/ `--target-stage`（既定 **3**）は**走る範囲**。「到達で終了」ではない。
  **引数なし実行＝プリセット生成**（Stage 3 のみ）。フルは `--start-stage 1 --target-stage 4` を明示
- `all_placed`（散布0）は**記録専用の指標**。高さ非依存なので `all_placed_height` と必ず併読
- 予算 60k / 35k / **5k** / 60k / 70k（Stage1-4 = 160,000、全5 = 230,000）

### モデル保存

- **1 run = プリセット 1 本**（全ステージ走破後）。定期 checkpoint / `checkpoint_every` は撤去
- 命名 `sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip`、ソート `(run_ts, steps)`、`fresh/` → `played/`
- **プリセット生成＝引数なし実行**（既定 start=target=3 / Stage3 steps=5,000、約35分）。
  「不器用→習得」の壁（実測 12k〜15k）のかなり手前で意図的に止める。
  ⚠️ 副作用として**フルカリキュラムは引数なしでは走らない**（範囲の明示が必要）

### 記憶モデル（3層）

| 層 | 実装 | 揮発 |
|---|---|---|
| 勘 | NN 重み | しない |
| 短期記憶 | 観測 dict に直近5手 | エピソード/セッション境界でクリア |
| 長期記憶 | `WeightedReplayBuffer` | `decay_rate 0.9999` / `weight_floor 0.001` |

- セッション跨ぎの揮発は `global_step += 経過日数 × steps_per_day`（既定5000）で一括減衰
- **train の `--resume` は撤去**。引き継ぎは live_server のスナップショットのみ

### その他

- 観測は **dict 一本化**（flat 撤去）
- **n_envs=1 / gradient_steps=1**（連動必須）
- 学習 env は `Monitor` で包む → **`rollout/ep_rew_mean` が出る**
- AWS: デモEC2 = live_server 主役、推奨 c6a.2xlarge（4コア/16GB）
- **ASG は撤去**。単一 EC2 を Lambda が start/stop する（§5.5）。Spot の自動フォールバック /
  中断復帰は手放したので手動対応（docs/aws_deployment.md §8）

### 未確定・注意（暫定）

- **AWS スケジュールは暫定**: プリセット生成=月初1日 5k、学習配信=平日10-18。確定値ではない
- **n_envs 不一致は依然として踏みやすい罠**: 既定は config と揃えたが、手動で変えた場合や
  古い n_envs のスナップショットを読ませた場合は `AssertionError` で
  **学習スレッドだけが落ち、配信は生き残る**（＝賢くならないことに気づきにくい）。
  既定の一致は `test_default_n_envs_equals_config_n_envs` で担保
- ~~学習の発散~~ は**解消済み**（§1.2.1 の実測検証。誤卒業の下流症状だった）

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

#### ステージ予算の配分（現行の既定値）

`stages[].steps` の**現行**既定値。合計 230,000（Stage 1-4 は 160,000）。

| Stage | steps | 根拠 |
|---|---|---|
| 1 | 60,000 | ゼロから基礎獲得。以降の全ステージがこの方策を継承する |
| 2 | 35,000 | 同じ cube のまま個数と高さが増えるだけ。転移が最も効く |
| 3 | **5,000** | **プリセット生成の標準値を兼ねる**（train 既定がここを走る）。習得の壁の手前で止める（§1.7） |
| 4 | 60,000 | 三角柱＝斜面。実測で退行が出るため厚め |
| 5 | 70,000 | 円柱＝転がる。既存方策が通用しない新規スキル |

Stage 1（ゼロから）と Stage 4/5（斜面・曲面）が重い。**Stage 3=5k が極端に小さいのは、この値が
プリセット生成の標準ステップ数を兼ねているため**（全 run で回すには短すぎる。要 --stage-steps 上書き）。

> 変遷: 当初 Stage3=40k → 25k → 10k → 最終的にプリセット標準として **5k**。
> 総量は当初 250k（2 steps/秒 で ~35h）想定だったが、n_envs=1 実測 約2.5 steps/秒 で 230k ≈ 26h。
> 「発散でそれ以上検証不能」という初期の懸念は §1.2.1 の卒業判定撤去で解消済み（下記検証）。

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
> Stage 3 を 40,000→25,000→10,000→（最終）5,000 に削り、Stage 4 を 45,000→60,000 に増やした。

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

### 1.7 プリセットの標準を「壁の手前」に（Stage 3 のみ・5,000 steps）＝**train の既定**

**train の既定そのものをプリセット生成にした**（`--start-stage` / `--target-stage` とも既定 **3**、
Stage 3 の steps=**5,000**）。**引数なし実行でプリセットが 1 本できる**。

変遷: 当初 12,000 → config 既定を 10,000 にして `--stage-steps` 不要化 → さらに 5,000 へ縮小し、
同時に stage 範囲の既定も 3 に変更して**フラグ完全不要**にした（月初の自動生成で扱いやすい）。

> **副作用**: フルカリキュラムは引数なしでは走らなくなった。
> `--start-stage 1 --target-stage 4` の明示が必要。
> また Stage 3 の steps は全カリキュラム run とも共用なので、フルで回すときは
> Stage 3 が 5,000 と短すぎる（`--stage-steps` で上書きする前提）。

**理由**: 2026-07-20 の Stage 3 のみ run（中断）で、**step 12,000〜15,000 に「不器用→習得」の
急激な壁**があると実測。

| step | ep_rew_mean | 高さ | success_rate |
|---|---|---|---|
| 11,506 | -0.64 | 0.086m | 0.00 |
| 14,687 | +6.33 | 0.436m | 0.63 |
| 21,794 | +12.4 | 0.607m | 0.97 |

30,000 まで走らせると success_rate 0.97 の「上手すぎる」モデルになりコンセプトに反する。
**5,000 で壁のかなり手前に止める**と「掴む・運ぶはできるが積めない子供」になり、ライブ（Stage 5）で
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

### 5.5 Auto Scaling Group の撤去 → 単一 EC2 の start/stop

| | 旧仕様 | 現行 |
|---|---|---|
| インスタンス管理 | ASG ×3（`min=0 / max=1`） | **単一 EC2 ×3**（作成後 stop） |
| 起動/停止 | Lambda が `desired_capacity` を 0⇄1 | Lambda が `ec2:StartInstances` / `StopInstances` |
| Lambda の payload | `{"asg_names": [...]}` | `{"instance_ids": [...]}` |
| Lambda env var | `ASG_NAMES` | `INSTANCE_IDS` |
| IAM | `autoscaling:UpdateAutoScalingGroup` 等 | `ec2:StartInstances` / `StopInstances` / `DescribeInstances` |
| state キー | `asg_names` | `instance_ids` |

**理由**: `min=0 / max=1` でオートスケーリングは一切しておらず、**実態は「起動/停止スイッチ」**
だった。目的に対して構成が過剰なので、単一 EC2 の start/stop に置き換えた。

**手放した機能（許容と判断し、手動対応する）**:

| 失った機能 | 代替 |
|---|---|
| Spot 在庫切れ時の複数タイプ自動フォールバック（Mixed Instances Policy + capacity-optimized） | `common.ps1` の `DemoType` / `LearnerType` を `LearnerFallbackCandidates` から手動で差し替えて再実行 |
| Spot 中断後の自動再起動 | 手動で `aws ec2 start-instances` |

いずれも `docs/aws_deployment.md` §8 トラブルシューティングに対処手順を記載した。

**副次的な利点**: ASG の scale-down は **terminate** だが、stop は **EBS を保持**する。
live_server のスナップショット（`replay_buffer.pkl` / `resume_state.json`）や `world_state` が
ディスク上に残るため、日次の停止→起動で状態が失われにくい。

> **Terraform 版（`infra-terraform/`）は ASG のまま温存**している。将来 ASG を復活させる際の
> 設計リファレンスとして意図的に残したもので、現行の `deploy/` とは差分がある
> （`main.tf` 冒頭にその旨を明記）。

### 5.6 本番インスタンスの呼称: `demo` → `live`

旧称の「デモ EC2 / `demo`」は、**推論を再生するだけのデモ機**だった頃の名残。現在この EC2 は
`serving.live_server` を回して**配信しながらバックグラウンドで学習を継続する本番機**であり、
「デモ」は実態と合わなくなっていたため `live` に統一した。

| 対象 | 旧 | 現行 |
|---|---|---|
| ECR リポジトリ | `block-stacker/demo` | `block-stacker/live` |
| EC2 / LT / SG 名 | `bs-demo` / `bs-demo-lt` | `bs-live` / `bs-live-lt` |
| user-data | `deploy/userdata/demo.sh` | `deploy/userdata/live.sh` |
| SSM パラメータ | `/bs/demo/private_ip` | `/bs/live/private_ip` |
| CloudWatch ロググループ | `/aws/ec2/bs-demo` | `/aws/ec2/bs-live` |
| コンテナ名 | `docker run --name demo` | `--name live` |
| Scheduler 名 | `bs-demo-start` / `-stop` | `bs-live-start` / `-stop` |
| state.json キー | `sg_demo_id` / `instance_ids.demo` | `sg_live_id` / `instance_ids.live` |
| `common.ps1` 変数 | `DemoType` | `LiveType` |

> **既存環境がある場合**は改名だけでは追従しない。`state.json` の `sg_demo_id` /
> `instance_ids.demo` は新スクリプトから読まれないため、キー名を手で書き換えるか
> `./99_destroy.ps1` → 再作成する。ECR リポジトリと SSM パラメータも旧名のまま残る。

**改名しなかった「demo」**（別物なので混同しないこと）:

| 対象 | 何か |
|---|---|
| `src/block_stacker/serving/demo_server.py` | AI なしの物理のみデモサーバ。クライアント開発用に現役 |
| `tools/demo_checkpoints.ps1` | ai_server による checkpoint 手動再生（開発用） |
| `docs/local_demo.md` | ローカル試運転手順 |
| `configs/training.yaml` の `demotion_enabled` | ステージ降格フラグ。文字列が偶然一致しているだけ |
| `infra-terraform/` 一式 | 凍結された ASG 版参照実装（§5.5）。現行経路ではないので追従させない |

### 5.7 `Dockerfile.learner` 単体 → `Dockerfile` の multi-stage（live / learner）

live 用イメージの `Dockerfile` は**設計書に名前だけあって実体が無く**、`block-stacker/live` を
ビルドできない状態だった。実体を作るにあたり、learner と別ファイルにするか共通化するかを検討し、
**1 本の `Dockerfile` に `base` / `learner` / `live` の 3 ステージ**を置く形に統合した。

| | 旧 | 現行 |
|---|---|---|
| live | （実体なし） | `docker build -t block-stacker/live .`（最終ステージ） |
| learner | `docker build -f Dockerfile.learner ...` | `docker build --target learner ...` |
| 依存リスト | Dockerfile.learner のみ | `base` ステージに 1 本化 |

**別ファイルにしなかった理由**: live_server は「配信しながらバックグラウンドで SAC を学習する」ため、
推論用の依存だけでは足りず stable-baselines3 / torch / tensorboard を learner と**丸ごと同じだけ**必要とする。
2 ファイルに分けると同一の依存リストが 2 本になって必ず drift する。共通ベースを別イメージとして
ECR に push する案（3 リポジトリ化）も考えたが、ビルド順の依存が増えるだけで利点が無いので採らなかった。

ベースイメージも `python:3.11-slim`（設計書の記載）→ `python:3.12-slim` に揃えた（ローカル `.venv` と同じ）。

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
