# ログ解読マニュアル

学習（`mvp2.train`）と推論/配信（`mvp3.ai_server`）が画面に出すログの読み方をまとめる。
「この行は何を言っているのか」「正常か異常か」をすぐ判断するための早見表。

> このマニュアルは**できるだけ専門用語を使わず**に書いている。ログに出る英単語
> （`actor_loss` など）はそのまま残し、意味をやさしい言葉で説明する。難しい言葉が
> 出てきたら §5 単語帳を見れば引ける。

---

## 0. ログの読み方の基本

ほとんどの行はこの形：

```
2026-06-12 21:11:39,186 [mvp2.train] Beginning training...
└─── 日付と時刻 ───┘     └─ 出どころ ─┘ └──── 内容 ────┘
```

「出どころ」（カッコ内の名前）でどこが出したログか分かる：

| カッコ内の名前 | 出どころ |
|-----------|---------|
| `mvp2.train` | 学習プログラム |
| `mvp3.ai` | 配信用の AI サーバ |
| `websockets.server` / `streaming.server` | 配信の通信部分 |

名前が付かない行（`pybullet build time: ...` や下の数値の表）は、使っている部品
（ライブラリ）が直接出しているもの。

> **PowerShell の赤い文字について**: `python.exe : ...` で始まる赤いかたまりは、
> PowerShell が**エラー用の出力を赤く表示しているだけ**で、異常とは限らない。
> 積み木の物理シミュの起動メッセージや軽い注意書きがここに出る。中身を読めばよい。

---

## 1. 学習ログ（`mvp2.train`）

### 1-1. 起動とステージ進行で出る行

既定では**オートカリキュラム（Stage 1→5 を自動進行）**。ログは「全体の起動まとめ」→
「ステージごとの開始・卒業」を Stage 5 まで繰り返す。**いま何ステージ目か**は
`=== Stage N: ... ===` の行で分かる。

```
pybullet build time: May 30 2026 15:47:27           ← 物理シミュの起動（動かす環境の数だけ出る）
2026-... [mvp2.train] MVP 2: SAC (curriculum)
2026-... [mvp2.train] Curriculum: stages [1, 2, 3, 4, 5], graduate at success_rate >= 0.60 over 30 eps
2026-... [mvp2.train] === Stage 1: Stage 1: cube だけ・低い目標 ===
2026-... [mvp2.train] Inventory: {'cube': 8}, target_height=0.240 (=満積み×0.60), h_high=0.075, h_low=0.025        ← この段階で使うブロック（cube 8 個）
2026-... [mvp2.train] Total timesteps (全ステージ合計の上限): 4000, n_envs: 6, subproc=True, stm_length=5
2026-... [mvp2.train] Memory system enabled: True   ← 記憶のしくみが ON
2026-... [mvp2.train] Beginning training (stage 1; 残り予算 4000 / 全体 4000)...  ← このステージの学習開始
Using cpu device                                    ← CPU で計算している
Logging to output\mvp2\tb\SAC_1                     ← グラフ用データ（カリキュラムを通して連続）
   …（学習が進む。数値のまとめ表が繰り返し出る → §1-2）…
2026-... [mvp2.train] Stage 1 graduated (success_rate=0.78).            ← 卒業！次ステージへ
2026-... [mvp2.train] === Stage 2: Stage 2: cube だけ・高い目標 ===
2026-... [mvp2.train] Inventory: {'cube': 15}, target_height=0.450 (=満積み×0.60), ...
   …（Stage 5 まで繰り返し）…
```

| 行 | 意味・見るところ |
|----|------|
| `MVP 2: SAC (curriculum)` | カリキュラム ON（既定）。`--no-curriculum` 実行なら `(single-stage: Stage 1...)` |
| `Curriculum: stages [...]` | これから順に学習するステージ番号と、卒業条件（成功率 ≥ 0.6 を直近 30 エピソード） |
| **`=== Stage N: ... ===`** | **いま学習中のステージ番号**。ここでステージが切り替わる |
| `Inventory: ..., target_height=0.240 (=満積み×0.60)` | そのステージの在庫と目標高さ（= 在庫満積み高さ × ratio） |
| `Beginning training (stage N; 残り予算 X / 全体 Y)` | そのステージの学習開始。Y=全ステージ合計の上限（`--total-timesteps`）、X=残り |
| `Stage N graduated (success_rate=...)` | 卒業 → 次ステージへ。届かず予算切れなら `Stage N did NOT graduate ... 中断` |
| `[graduation] all blocks placed (散布0) -> GRADUATE` | **散布0を達成して即卒業**（成功率を待たない fast-track） |
| `pybullet build time` | 物理シミュの起動表示。**`--n-envs 6` なら 6 回出るのが正常**（6 個同時に動かすから） |
| `Total timesteps / n_envs / stm_length` | 実際に使われた設定値。`stm_length=5` なら「直近 5 手の記憶」を AI が見ている |
| `Memory system enabled: True` | 記憶のしくみ（重要度＋高さ補正＋減衰＋重みつき抽選）が効いている |
| `Using cpu device` | `cpu` と出れば正常（このパソコンは CPU で学習する前提） |

### 1-2. 数値のまとめ表（ときどき出る）

少し進むごとにこの表が出る。**平均報酬（`rollout/ep_rew_mean`）は出ない**（Monitor 未装着）ので、
スコアの伸びを見たいときは TensorBoard（§4）でグラフを見る。
**カリキュラム実行中は `curriculum/` の欄が毎回出て、いま何ステージ目かが一目で分かる。**

```
---------------------------------
| curriculum/        |          |
|    stage           | 1        |   ← いま学習中のステージ番号
|    success_rate    | 0.40     |   ← 直近30エピソードの成功率（卒業は 0.6 で）
|    episodes_seen   | 24       |
| rollout/           |          |
|    success_rate    | 0.45     |   ← SB3 自動。直近~100エピソードの成功率（卒業判定には未使用）
| time/              |          |
|    episodes        | 24       |
|    fps             | 2        |
|    time_elapsed    | 192      |
|    total_timesteps | 474      |
| train/             |          |
|    actor_loss      | -22.5    |
|    critic_loss     | 0.0928   |
|    ent_coef        | 0.723    |
|    ent_coef_loss   | -3.82    |
|    learning_rate   | 0.0003   |
|    n_updates       | 1088     |
---------------------------------
```

#### `curriculum/`（いまどのステージか・卒業の進み）— カリキュラム実行時のみ

| 項目 | 意味 | 見方 |
|-----------|------|------|
| `stage` | いま学習中のステージ番号（1〜5） | スクロール中でも一目で分かる |
| `success_rate` | 直近 `window`（=30）エピソードの「目標高さ到達」成功率 | **`threshold`（既定 0.6）に届くと卒業** |
| `episodes_seen` | このステージで終わったエピソード数 | window に満たないうちは success_rate が安定しない |

> **卒業は2通り（OR）**: ① **散布0（全ブロックを積み切る）を達成したら即卒業**
> （`[graduation] all blocks placed (散布0) -> GRADUATE` のログが出る。成功率を待たない）。
> ② 上の `success_rate`（目標高さ到達）が 0.6 以上。どちらかで次ステージへ。

> **`rollout/success_rate` も出るが別物**: env が `info["is_success"]` を返すので SB3 が自動で
> 出す成功率（直近 **約100** エピソード）。**卒業判定に使うのは `curriculum/success_rate`（30）**の方で、
> `rollout/` は「もっと長い目で見た参考値」。窓が違うので両者の数値は少しズレる。
> （`rollout/ep_rew_mean`＝平均報酬は Monitor 未装着のため出ない。`success_rate` だけ自動で出る）

#### `time/`（どれだけ進んだか）

| 項目 | 意味 | 見方 |
|-----------|------|------|
| `episodes` | 終わったゲーム数（崩落や手数切れで 1 ゲーム） | 増えていれば進んでいる |
| `fps` | 1 秒あたり何手進むか | この重い物理計算では**数〜数十が普通**。低くても異常ではない |
| `time_elapsed` | 開始からの経過秒数 | — |
| `total_timesteps` | AI が動いた手数の合計 | 目標（`--total-timesteps`）に向かって増える。進み具合の目安 |

#### `train/`（学習の中身）— 最初の 200 手を過ぎてから出る

ここの英単語はそのままログに出る名前。意味だけ覚えればよい。

| 項目 | これは何か | 良い兆候 / 悪い兆候 |
|-----------|------|------|
| `actor_loss` | AI の「動き方」を直すための数値。SAC ではふつうマイナス | 値そのものは気にしなくてよい。**急に巨大化したり `nan` になったら異常** |
| `critic_loss` | AI の「この手は良い/悪い」という**採点のズレ具合** | 小さく落ち着いていれば良い。**どんどん大きくなったら異常** |
| `ent_coef` | AI が「**いろいろ試す**」度合いの調整つまみ。最初 ~1.0 | **だんだん下がる**のが良い（試行錯誤 → 良い手に絞る）。例: 0.79→0.72 は順調 |
| `ent_coef_loss` | 上のつまみを自動調整するための内部数値 | マイナスがふつう。`nan` なら異常 |
| `learning_rate` | 1 回でどれだけ学ぶか（学ぶ歩幅） | `0.0003` 固定 |
| `n_updates` | AI の頭の中を直した回数の合計 | 増えていれば学習中。`0` のままなら、まだ助走期間（最初の 200 手） |

> **ひとめ早見**: `total_timesteps` と `n_updates` が増え続け、`critic_loss` が急増せず、
> `ent_coef` がゆっくり下がっていれば順調。`nan`（計算が壊れたサイン）が出たら異常。

### 1-3. 保存と終了

```
（学習中、output\mvp2\fresh\sac_<YYYYMMDD-HHMMSS>_<手数>_steps.zip が total_timesteps の 20/40/60/80/100% 地点で自動保存）
2026-... [mvp2.train] 学習完了: 最終モデル = fresh/ の最大ステップ checkpoint (sac_final.zip は廃止)
2026-... [mvp2.train] 長期記憶を保存: output\mvp2\replay_buffer.pkl
2026-... [mvp2.train] Resume state saved: output\mvp2\resume_state.json (next_stage=2, completed=[1])
```

- **`sac_final.zip` は廃止**。最終モデルは `fresh/` の最大ステップ checkpoint が相当する。
  デモ（`ai_server`）は起動時に `find_latest_checkpoint` で最新 run の最大ステップ checkpoint を自動選択する。
- **`replay_buffer.pkl`（長期記憶）と `resume_state.json`** は毎回保存される。次回 `--resume` で
  NN 重み・長期記憶・カリキュラム進捗（`next_stage_id`）を引き継いで再開できる。
- checkpoint 名 `sac_<YYYYMMDD-HHMMSS>_<手数>_steps.zip` の `<YYYYMMDD-HHMMSS>` は同一 run の
  5 本で共通（`run_ts`）。`<手数>` はカリキュラムを通して連続した総タイムステップ（`--n-envs 6` だと
  端数が生じ `19998` のような数になる）。
  ソートキーは `(run_ts, steps)` 昇順＝学習順。[`tools/demo_checkpoints.ps1`](../tools/demo_checkpoints.ps1) /
  `local_loop.ps1` がこの順で `fresh/` の checkpoint を再生する（再生環境は常に最終ステージ）。
- checkpoint は学習の 20/40/60/80/100% 地点にほぼ 5 本生成される（`checkpoint_splits=5`）。
  最終ステージ卒業後も `total_timesteps` まで走り続けるため、checkpoint が欠落することはない。

---

## 2. 配信 / AI サーバのログ（`mvp3.ai`）

### 2-1. 起動

```
2026-... [mvp3.ai] demo stage: id=5 '...' inventory={...} target=0.444 h_high=0.320 h_low=0.120  ← 常に最終ステージ
2026-... [mvp3.ai] loading model: output\mvp2\fresh\sac_20260627-143022_4000_steps.zip   ← 無指定なら find_latest_checkpoint で最新 run の最大ステップを自動選択
2026-... [mvp3.ai] model loaded: n_params=856178      ← AI の頭脳の部品数（壊れてなければ毎回同じ数）
2026-... [mvp3.ai] short-term memory length: 5         ← 学習時と同じ「直近 5 手の記憶」設定
2026-... [mvp3.ai] setting up world                    ← 積み木の世界を準備
2026-... [mvp3.ai] spawned 15 blocks for stage 5: {...}  ← そのステージの在庫どおりに全形状を散布
2026-... [websockets.server] server listening on 0.0.0.0:8765
2026-... [streaming.server] WebSocket server listening on ws://0.0.0.0:8765   ← Godot はここにつなぐ
```

> **起動してすぐ落ちる場合**: モデルが壊れているか、AI が見る情報の形が学習時とずれている。
> `short-term memory length` が学習時と違う、学習後に設定ファイルをいじった、などを疑う。

### 2-2. AI の 1 手（2 行で 1 セット）

1 手ごとに「これからやること」→「やった結果」の 2 行が出る。

```
2026-... [mvp3.ai] AI: pickup body=7 (0.51,-0.35,0.02) → place (-0.75,0.62,0.07), tower=0.050, lift=0.075
2026-... [mvp3.ai] post-action: tower_h=0.050, best=0.050, event=height_record
```

#### 1 行目 `AI: pickup ... → place ...`（これからやること）

| 部分 | 意味 |
|------|------|
| `pickup body=7` | つかむブロックの番号（床のブロックのうち、ねらった場所に一番近いもの） |
| `(0.51,-0.35,0.02)` | そのブロックの今の位置（左右・奥行き・高さ、単位はメートル） |
| `place (-0.75,0.62,0.07)` | AI が**置こうとしている**目標の位置 |
| `tower=0.050` | **置く前**のタワーの高さ（記憶の「高く積めた時の上乗せ」にも使う値） |
| `lift=0.075` | 運ぶとき持ち上げる高さ（今のタワーにぶつからないための余裕） |

#### 2 行目 `post-action: ...`（やった結果）

| 部分 | 意味 |
|------|------|
| `tower_h` | **置いた後**のタワーの高さ |
| `best` | この起動中での最高到達の高さ |
| `event` | この手の結末（下の表）。学習時と同じ判定 |

`event` の種類：

| event | 意味 | 記憶での重要度（学習時） |
|-------|------|------|
| `collapse` | 崩した | 1.0（いちばん強く残る） |
| `failure` | 進まずに打ち切り | 0.7 |
| `height_record` | 高さの新記録 | 0.5 |
| `success` | 置けた（新記録ではない） | 0.3 |
| `no_progress` | 何も起きなかった | 0.1 |

### 2-3. その他よく出る行

```
2026-... [mvp3.ai] no scattered block to pick — waiting 2.0s
        → 床に拾えるブロックが無い／遠くて、いったん待っている。

2026-... [mvp3.ai] COLLAPSE (h_before=0.105, h_after=0.020, dispersion=0.80)
        → 崩れた。高さが 0.105→0.020 に落ち、崩れた割合(dispersion)が一定を超えた合図。
          配信側に「崩れた」と知らせる。崩れてもブロックはその場に残す設計（並べ直さない）。
```

---

## 3. 成長を「ログだけで」読む早見表

Godot の画面を見られないとき、ログから上達度を推し量る目安：

| こうなったら | こう読める |
|------|------|
| `event=success` / `height_record` が増える | 置けるようになってきた（中盤以降） |
| `event=no_progress` ばかり | まだ掴めない／置けない（序盤 5,000〜30,000 手では正常） |
| `COLLAPSE` が減る | タワーを崩さなくなってきた |
| `tower=` / `best=` の値が回を追って上がる | 安定して高く積めるようになっている |
| `place(...)` の位置がタワーの近くに集まる | 置き場所の狙いが定まってきた（序盤は変な所に飛ぶ） |

---

## 4. TensorBoard（数値の動きをグラフで見る）

ターミナルの表は「今この瞬間」だけ。**時間ごとの変化**をグラフで見たいなら TensorBoard：

```powershell
.\.venv\Scripts\python.exe -m tensorboard.main --logdir output\mvp2\tb
# ブラウザで http://localhost:6006 を開く
```

`critic_loss`・`actor_loss`・`ent_coef` などの変化が線グラフで見られる。
うまく学習が進むほど `ent_coef` が下がり、`critic_loss` が落ち着いていく。

---

## 5. 単語帳（用語集）

ログやこのマニュアルに出る言葉を、積み木 AI の話に置きかえてやさしく説明する。

### 5-1. 学習まわり

| 言葉 | 読み方 | やさしく言うと |
|------|--------|--------------|
| **SAC** | サック | この AI の学習のやり方の名前。「いろいろ試しながら上達する」タイプ |
| **モデル** | — | 学習で出来上がった AI 本体（ファイルとして保存できる） |
| **方策 / policy** | ほうさく | 「今の盤面でどう動くか」を決める AI の判断 |
| **actor（アクター）** | — | 「どう動くか」を出す担当 |
| **critic（クリティック）** | — | 「その動きは良かった？」を採点して actor を導く担当 |
| **actor_loss** | — | actor を直すための数値。SAC ではふつうマイナス。急変や `nan` でなければ気にしなくてよい |
| **critic_loss** | — | critic の採点のズレ具合。小さく安定が良い。増え続けたら異常 |
| **ent_coef** | — | AI が「いろいろ試す」度合いの調整つまみ。学習が進むと下がる（良い手に絞る）と良い |
| **learning_rate** | — | 1 回でどれだけ学ぶか（学ぶ歩幅）。ここでは 0.0003 固定 |
| **nan** | ナン | 計算が壊れたサイン。出たら異常 |
| **timestep / total_timesteps** | — | AI が 1 手動いた回数 / その合計。進み具合の目安 |
| **episode（エピソード）** | — | 1 ゲーム分（崩すか手数切れまで）。1 ゲーム最大 30 手 |
| **n_updates** | — | AI の頭の中を直した回数の合計。増えていれば学習中 |
| **learning_starts** | — | 最初の 200 手は「データ集めだけ」して学習しない助走期間 |
| **checkpoint** | チェックポイント | 学習途中の AI を保存したファイル。成長の「コマ送り」に使う |
| **カリキュラム** | — | Stage 1→5 と難易度を段階的に上げながら学習するしくみ（既定 ON） |
| **ステージ / stage** | — | 難易度の段（1〜5）。段が上がると在庫数・目標高さ・形状が増える |
| **卒業 / graduation** | — | そのステージの成功率が条件に達して次ステージへ進むこと |
| **success_rate（成功率）** | — | 直近 30 エピソードのうち「成功」の割合。**0.6 で卒業**（`threshold`） |
| **目標高さ / target_height** | — | そのステージのゴール高さ。＝ 在庫を全部縦積みした高さ × `ratio`（既定 0.6） |
| **成功（is_success）** | — | そのエピソードで「目標高さ到達」したか（成功率の対象）。※散布0（全部積み切り）は別枠で**即卒業** |

### 5-2. 積み木ゲームの中身

| 言葉 | やさしく言うと |
|------|------|
| **環境 / env** | 積み木のゲーム 1 個分。学習中は速くするため複数同時に動かす |
| **記憶のしくみ** | AI に「子供っぽい記憶」を持たせる工夫。3 種類ある（下の 3 つ） |
| **勘** | 体で覚えた感覚 ＝ AI の頭脳そのもの（記憶 その1） |
| **短期記憶 / stm** | 「ついさっきの数手」。直近 5 手を AI が見られるようにしたもの（記憶 その2） |
| **長期記憶** | 過去の経験を「重要度つき」で貯めておく記憶（記憶 その3） |
| **重要度（初期重み）** | 経験を貯めるときの大事さ。崩した経験 1.0 が最大、何も起きなかった 0.1 が最小 |
| **高さの上乗せ補正** | 高いタワーで起きた経験ほど重要度を底上げするしくみ |
| **event（できごと）** | その 1 手の結末。崩した/打ち切り/新記録/置けた/何も無し の 5 種 |
| **tower / tower_h / best** | タワーの高さ（置く前）/ 置いた後の高さ / その起動中の最高 |
| **dispersion** | 崩れ判定に使う「元のタワーのうち崩れた割合」。0〜1 |
| **lift** | ブロックを運ぶとき持ち上げる高さ（ぶつからない余裕） |
| **body / body ID** | 物理シミュ上のブロックの番号。`pickup body=7` の 7 |
| **持続ワールド** | 崩れてもブロックを並べ直さず、その場に残す設計 |
| **heightmap** | 盤面の高さを格子状に写した「高さの地図」。AI が見る情報の一部 |

### 5-3. 道具・土台

| 言葉 | やさしく言うと |
|------|------|
| **PyBullet** | 積み木の落下や衝突を計算する物理シミュ。`pybullet build time` は起動表示 |
| **n_envs** | 積み木のゲームを同時に何個動かすか。`--n-envs 6` で 6 個（起動表示も 6 回出る） |
| **TensorBoard** | 学習の数値をグラフで見る道具（§4） |
| **WebSocket** | AI サーバと Godot 画面をつなぐ通信。`ws://127.0.0.1:8765` |
| **Godot** | 積み木を 3D で表示するクライアント（画面側） |
| **モデルファイル** | 学習済み AI を保存した `.zip`。AI サーバはこれを読み込んで動かす |

---

## 関連

- 学習プログラム: [`src/block_stacker/mvp2/train.py`](../src/block_stacker/mvp2/train.py)
- AI サーバ: [`src/block_stacker/mvp3/ai_server.py`](../src/block_stacker/mvp3/ai_server.py)
- ローカル試運転手順書: [`docs/local_demo.md`](local_demo.md)
- 設計書（記憶のしくみ）: [`docs/block_stacker_design.md`](block_stacker_design.md) §3 層記憶
- デプロイ手順書（記憶の詳しい仕様）: [`docs/aws_deployment.md`](aws_deployment.md) §付録 E §1
