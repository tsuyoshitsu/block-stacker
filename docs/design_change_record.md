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

### 1.3 学習の終了条件: 全ステージ完走 → `--target-stage` 卒業で打ち切り

| | 旧仕様 | 現行 |
|---|---|---|
| 既定挙動 | Stage 1→5 を全走、budget 打ち切りまで継続 | `--target-stage`（既定 **4**）卒業時点で終了しプリセット保存 |
| 全ステージ完走 | 既定 | `--target-stage 5` |
| budget 完走（旧挙動の再現） | 既定 | `--target-stage 9999` 等（到達しない値） |

**変更**: `a604878`（機能追加）/ `4abb5a4`（docs 反映）

### 1.4 checkpoint ファイル名

| | 旧仕様 | 現行 |
|---|---|---|
| 命名 | `sac_<steps>_steps.zip` | `sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip` |
| 最終モデル | `sac_final.zip` | **廃止**。`fresh/` の最大ステップ checkpoint が最終モデル相当 |
| ソート基準 | ファイル名昇順 | `(run_ts, steps)` |

**変更**: `4257d88`（2026-06-27）
**理由**: run をまたいで checkpoint が `played/` に蓄積されるため、run を識別できないと
ファイル名が衝突し、ソート順も学習順と一致しなくなる。

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
