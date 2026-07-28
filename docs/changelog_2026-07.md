# 変更まとめ 2026年7月

対象期間: 2026-07-01 〜 2026-07-31（実際の作業は **07-13 〜 07-28**、コミット 37 本）。

この 1 ヶ月で、プロジェクトは「**学習して、できたモデルを後から再生する**」設計から
「**配信しながら学習し続ける**」設計へ転換した。それに伴い、学習の終了条件・AWS 構成・
運用ツール・docs のすべてが連鎖的に書き換わっている。

現行仕様の基準は [`CLAUDE.md`](../CLAUDE.md)、旧仕様の読み替えは
[`design_change_record.md`](design_change_record.md) を参照。

---

## 1. 設計転換: 学習と配信の融合（live_server）

**何を**: 配信サーバ `live_server.py` を Step A〜E の 5 段階で新規実装した（07-13, 1 日）。

| Step | コミット | 内容 |
|---|---|---|
| A | `6b769e4` | スケルトン。WebSocket 配信のみ（240Hz PhysicsBroadcaster + ai_driver） |
| B | `5c2f340` | バックグラウンド学習スレッド + `WeightSyncer`（学習側の重みを配信側へ同期） |
| C | `249f70f` | resume 統合。`replay_buffer.pkl` の読み込みと経過日数ぶんの時間減衰 |
| D | `f1fb16b` | シャットダウン処理 + スナップショット保存 |
| E | `ee7fa8b` | スモークテスト + parser 分離 |

**なぜ**: 従来は `training.train` が WebSocket を持たず、学習中はクライアントから何も見えなかった。
モデルも走破後まで書き出されないので、`ai_server` を並走させても古いモデルを映すだけだった。
「AI の成長をリアルタイムで見せる」というサービスの根幹が成立していなかった。

**結果**: メインスレッド（asyncio）が 240Hz 物理と配信を回し、バックグラウンドスレッドが
独立した PyBullet で `SAC.learn()` を回す二世界構成。視聴者は「昨日の続きから少しずつ上達する AI」を
見られるようになった。運用手順は [`live_mode.md`](live_mode.md)（`c4a4617` で新規作成）。

関連: `ece3e32` で argparse 既定値を運用値（`--n-envs 1` / `--sync-every 50`）に揃えた（→ §7）。

---

## 2. 学習の終了条件: 卒業判定の撤去 → 固定ステップ制

この月で**最大の設計変更**（`8e3e642`, 15 files / +1136 -666）。3 段階で進んだ。

### 2-1. `--target-stage` の導入（`a604878`, 07-13）

指定ステージを卒業した時点で学習を終了しプリセットを保存する仕組みを追加。
同時に `checkpoint_splits`（total_timesteps 依存）→ `checkpoint_every`（絶対ステップ間隔）に置換。

### 2-2. 卒業判定そのものの撤去（`8e3e642`, 07-21）

**何を**: 卒業ゲートを完全に撤去し、各ステージが `stages[].steps` の予算ぶんだけ走って
成績によらず次へ進む固定ステップ制に置き換えた。

**なぜ**: 卒業ゲートが誤検出していた。`all_placed`（散布0）は「縦接触で連結された成分」から
計算されるため、**横に広い低い構造でも成立する**（8 個をレンガ積みにした高さ 0.100m でも成立する
ことを実測確認）。結果、`success_rate` が 0 のまま Stage 1→4 が数千ステップで飛んでいた。

さらに 1/10 スケール実行で、**ent_coef / critic_loss の発散も同じ原因の下流症状**だと判明した
（`critic_loss` 最大 2.17e15 → ゲート撤去後は 5.11）。SAC 自体の問題ではなく、
スキルゼロの方策を最難ステージに放り込んでいたことが原因だった。

**結果**:
- `GraduationCallback` を削除。`StageMonitorCallback` は指標を記録するだけで学習を止めない。
- `--stage-steps` を追加（単一値なら一括、カンマ区切りなら実行ステージへ順に割当。要素数不一致はエラー）。
- `--target-stage` / `--max-stage` は「到達したら終了」ではなく単なる範囲指定に意味が変わった。
- 指標に `curriculum/all_placed_height` と `tower_height_mean` を追加。
  `all_placed` 単体では本物の塔かレンガ積みか判別できないため、**必ず高さと併読する**。
- 学習 env を `Monitor` で包むよう修正。これが無く `rollout/ep_rew_mean` が黙って欠落しており、
  報酬曲線が TensorBoard から丸ごと消えていた。
- 散布0 達成時に全ブロックを再配置してラウンドを継続するようにした。従来は課題を完遂した
  エピソードが空振りのまま timeout_penalty を食らい、**failure として強い負の記憶に焼かれていた**。
- 定期 checkpoint を撤去（1 run = プリセット 1 本）。`train --resume` も撤去（毎 run ゼロから）。
- コンセプト（最適なモデルではなく不器用な子供を見せる）に合わせ `n_envs` 8→1、
  `gradient_steps` 8→1（遷移あたりの更新比率を保つため常に同値に保つ）。

### 2-3. プリセット生成を train の既定に（`c59b5ad`, 07-27）

**何を**: `--start-stage` 3 / `--target-stage` 3 を既定にし、**引数なしの `train` がそのまま
プリセット生成**（Stage 3 のみ）になるよう反転させた。Stage 3 の steps は 10,000 → **5,000**。

**なぜ**: プリセット生成は月次で live_server の種を作る運用の本線なのに 2 つのフラグが必要で、
逆にフラグ無しの実行は誰も望まない 18 時間のフルカリキュラムが走っていた。
5,000 は実測した「不器用→習得」の壁（step 12,000〜15,000）のかなり手前で、
ep_rew マイナス・success_rate 0.00 の「掴む・運ぶはできるが積めない子供」を意図的に残す値。

**結果**: 実測 2.5 steps/秒 で約 35 分。フルカリキュラムは
`--start-stage 1 --target-stage 4`（160,000 steps ≒ 18 時間）の明示が必要になった。
テストは既定値が「プリセット生成を意味する」ことを検証しており、start=1/target=4 に戻すと失敗する。

---

## 3. リポジトリ整理（07-14 に集中）

| コミット | 内容 |
|---|---|
| `52301ea` | **flat 観測パスを撤去し dict 一本化**。`observation_format` 引数、`observation_dim()`、`pack_observation_flat()` を削除（-138 行） |
| `286fa1d` / `81959ff` | コード・コメント・config から「MVP N」マイルストーン表記を撤去（16 files） |
| `6b3b462` | `ShortTermMemory` を `ai_server` から `serving/stm.py` へ分離 |
| `5790f34` | 未使用スタブ `flatness_bonus` / `flatness_scale` を削除 |
| `8ca2d0e` | **発散モデルの削除**（critic_loss 1.4e15 / ent_coef 5400+）。`find_latest_checkpoint` が最新 run として自動選択するため、放置すると再生に使われてしまう。あわせて MVP 0 時代の遺物 Dockerfile（存在しないモジュールを CMD に持ち、uv pip install で開発規則にも違反）を削除 |
| `6562ea3` | `ai_server._resolve_model_path` の無効な fallback パスを削除 |

**結果**: 観測形式が dict のみになり、学習・推論・テストの分岐が消えた。

---

## 4. 物理演算

7 月の変更は **`95dbc5b` の 1 件のみ**。`collapse_detection`（`tower_dispersion_ratio=0.5` /
`cooldown_after_collapse=2.0`）を `physics.yaml` に明示セクションとして起こした。
従来は `data.get('collapse_detection', {})` で暗黙の既定値が使われており、
**チューニング対象として見えていなかった**のが理由。

> 摩擦係数（`block_to_block` 0.6→0.45、`block_to_ground` 0.8→0.5）や CCD 有効化といった
> 実質的な物理調整は **6 月**（`cfa9c95` / `038e467` / `ec31aa0`）に済んでおり、7 月には入っていない。

---

## 5. AWS 構成の見直し（07-27 に集中）

### 5-1. デプロイ成果物を live_server 設計に追従させる（`3639fca`）

docs は「demo EC2 は live_server を動かす」に更新済みだったが、**デプロイ成果物が追従していなかった**。
監査で 4 件の乖離が見つかり、いずれも旧アーキテクチャをそのまま出荷しかねないものだった。

- `demo.sh` が `ai_server`（推論専用）を起動していた → デプロイしても**学習しない配信**になる
- モデルを `models/latest.pt` にリネームしてアップロードしていた →
  `find_latest_checkpoint` は `sac_*_steps.zip` を glob するので**サーバから見えない**
- `learner.sh` / `Dockerfile.learner` に `--total-timesteps 1000000 --n-envs 8` が残存

### 5-2. Auto Scaling Group の撤去（`27c115c`）

**何を**: 3 つの ASG を撤去し、ロールごとに単一 EC2 を作って即 stop、以降は Lambda が
start/stop する方式に置き換えた。

**なぜ**: 3 つとも min=0/max=1 で**一度もスケールしていなかった**。実態は Lambda が
`desired_capacity` を叩く on/off スイッチで、その周りに大量の機構が巻かれていただけ。

**結果**:
- `lambda/handler.py` を `update_auto_scaling_group` → `start_instances`/`stop_instances` に変更
- IAM を `autoscaling:*` → `ec2:StartInstances/StopInstances/DescribeInstances` に絞った
- 意図的に手放した機能（手順は deploy ガイドのトラブルシュート表に追記）:
  Spot 在庫切れ時のインスタンスタイプ自動フォールバック / Spot 中断後の自動再起動
- **副次的な利得**: ASG のスケールダウンは terminate だが stop は EBS を保持するので、
  live_server のスナップショットが日次の stop/start をまたいでディスク上に残る
- `tests/test_lambda_handler.py`（10 件）を追加。boto3/jpholiday は .venv に無いので
  skip ではなく `sys.modules` スタブで実行する（skip では ASG API への回帰を検知できないため）
- `infra-terraform/` は ASG 版を参照実装として温存し、`main.tf` 冒頭に現行ではない旨を明記

### 5-3. `demo` → `live` の広域改名（`dfaf4c2`）

推論を再生するだけのデモ機だった頃の名残を、実態（配信＋学習の本番機）に合わせた。
ECR リポジトリ・EC2/LT/SG 名・user-data ファイル名・SSM パラメータ・CloudWatch ロググループ・
コンテナ名・Scheduler 名・state.json キー・`common.ps1` の変数まで横断。

改名しなかった `demo`（別物）: `serving/demo_server.py`（AI なしの物理デモ・現役）、
`local_demo.md`、`demotion_enabled`、`infra-terraform/`（凍結参照）。

### 5-4. live 用 Dockerfile の新設（`6f8f63f`）

`block-stacker/live` の Dockerfile は**設計書に名前だけあって実体が無く**、イメージを
ビルドできなかった。learner と別ファイルにせず、`base` / `learner` / `live` の
3 ステージ multi-stage に統合した（live_server は配信しながら学習するので依存が learner と
完全に同一で、分けると依存リストが 2 本になり必ず drift するため）。

### 5-5. Fargate 検討 → 不採用（`7e90606`）

ECS Fargate への移行を検討したが **live は EC2 のまま**と決定。理由:

1. **物理コアを専有できない** — インスタンス選定の第一の軸が「240Hz の `world.step()` に
   物理コアを 1 つ専有させる」であり、core pinning できない Fargate では選定根拠が成立しない
2. **永続ディスクが無い** — 1.6GB の `replay_buffer.pkl` を毎日 S3/EFS と往復させる必要がある
3. **安定した private IP が無い** — SSM 経由の IP discovery が使えず Cloud Map / NLB が要る
4. **コストがほぼ同額** — Fargate Spot ≒ $26/月 vs EC2 Spot $22.9/月

learner は Fargate が素直に合うが、差額が月 ¥60 程度で第二のデプロイ経路を抱える対価に
見合わないため、live 側に ECS 化の動機が出たら同時に再検討する。

---

## 6. バグ修正

### 6-1. 停止・Spot 中断時にスナップショットが退避されていなかった（`a75192f`）

Spot 中断 terminate で**その日の学習（最大 8 時間分）が EBS ごと失われる**状態だった。
原因は 2 段になっていた。

| | 症状 | 原因 |
|---|---|---|
| ① | 退避対象が `world_state/` だけ | 中断ハンドラが `/opt/bs/state/` しか sync せず、snapshot の置き場所 `/opt/bs/models/` が漏れていた |
| ② | **そもそも snapshot が書かれていない** | `docker stop` の SIGTERM は Python 既定で即時終了。`_save_live_snapshot()` を呼ぶ `finally` が回らない。①だけ直しても古い内容が上がるだけだった |

修正:
- `install_shutdown_handlers()` で SIGTERM/SIGINT を `--duration` 経過と同じ shutdown パスへ接続
- 学習スレッドの join を 15 秒 → 60 秒（1.6GB の書き出しに実測 10〜20 秒かかり切れていた）
- `bs_flush_s3.sh` を新設し **`docker stop -t 60` → `models/` + `world_state/` を sync** の順に統一。
  Spot ハンドラと停止時 unit（`bs-flush.service`）の両方から呼ぶ
- restart policy を `unless-stopped` → **`always`**。停止時に `docker stop` するため、
  `unless-stopped` だと翌営業日の start でコンテナが復帰しない

回帰は `tests/test_userdata_live.py`（12 件）で固定。実 AWS を叩けないのでスクリプト本文への静的検査。

### 6-2. live_server の既定 n_envs が config と不一致（`ece3e32`）

argparse の既定（`--n-envs 4` / `--sync-every 500`）が実際の運用値と一致しておらず、
n_envs のズレは**罠**だった。SAC の `set_env()` は n_envs 不一致で `AssertionError` を投げるが、
**落ちるのはバックグラウンド学習スレッドだけ**で配信は続く。症状が
「配信は正常なのにモデルが一向に上達しない」になり、実際に 1 セッション中に 2 回踏んだ。

`training.yaml` を読んで既定値と一致することを検証するテストを追加し、
config だけ変えた場合も検知できるようにした。

### 6-3. その他

- `6562ea3` — `ai_server._resolve_model_path` の無効 fallback パス
- `e372a5f` — `demo_server.py` / `test_client.py` の docstring に `.venv\Scripts` が入っており
  Python 3.12 が `SyntaxWarning: invalid escape sequence '\S'` を出していた（raw string 化で解消）

---

## 7. デッドコード・ツール整理（07-27〜28）

### 7-1. 死んだ設定キーと関数（`00cb267`）

卒業判定と定期 checkpoint の撤去で読み手を失ったものを削除した。
「設定できるように見えて効かない」ものは誤設定を招くため。

| 撤去物 | 状態 |
|---|---|
| `graduation.threshold` / `BS_GRADUATION_THRESHOLD` | `resolve_graduation` が返していたが**全呼び出し元が捨てていた**。戻り値を `(window, ratio)` の 2-tuple に |
| `graduation.rule` / `curriculum.demotion_enabled` / `stages[].stable_duration` | 参照ゼロ |
| `episode_reset_strategy` / `episode.timeout_treated_as` | 参照ゼロ |
| `list_checkpoints_sorted()` | 本番呼び出し元ゼロ（テストだけが生かしていた）|

`graduation` というキー名自体は残した。`window` / `ratio` は現役で、リネームすると
env var の互換も切れるため。名前に反して卒業判定が無いことはコード・設定・docs に注記済み。

### 7-2. `tools/` 3 本 → 1 本（`364c6db`）

いずれも「定期 checkpoint で成長系列ができる」時代の設計だったため役割を洗い直した。

| 旧 | 現行 |
|---|---|
| `advance_day.ps1`（日次 fresh→played ローテーション配信） | **撤去**。live_server に置き換わった |
| `demo_checkpoints.ps1`（対話選択） | `replay_checkpoints.ps1`（既定モード）へ統合 |
| `local_loop.ps1`（1 巡再生） | `replay_checkpoints.ps1 -All` へ統合 |

> **注意**: 定期 checkpoint は撤去したが、**live_server はセッション終了ごとに `fresh/` へ
> 1 本追加する**（`played/` へは移さない）。平日運用なら月 20 本前後の成長系列が溜まるので、
> 通し再生の用途自体は消えていない。「1 run = 1 本だから再生ツールは不要」と判断しないこと。

### 7-3. `training/eval.py` は残す（ただし陳腐化を修正）（`364c6db`）

配信スタックを立てずにモデルを数値評価できる唯一の経路なので残した。
ただし評価する世界を直書きしていて config に追従しておらず、
`inventory={"cube":5}`（実際の Stage 1 は 8）、`h_high/h_low=0.10/0.03`（実際は 0.075/0.025）、
`max_steps=10`（実際は 30）と**どのステージとも一致しない世界**で評価していた。
`ai_server` と同じ解決（`training.yaml` から引く／既定は最終ステージ／`--stage` で上書き）に揃えた。

### 7-4. `_self_stop_instance()` の削除（`364c6db`）

ログを出すだけのスタブだった。EC2 の start/stop は EventBridge → Lambda の責務に確定しているので、
プロセス側に自己停止の口を残す理由がない。

---

## 8. docs の全面整合

コードの変更に対して docs が 3 度にわたり追従した。7 月に最も触られたファイルは
`aws_deployment.md`（16 回）/ `block_stacker_design.md`（15 回）/ `design_change_record.md`（13 回）。

| コミット | 内容 |
|---|---|
| `5420b84` | **`tech_stack.md` 新規作成**（使用技術一覧、305 行） |
| `c4a4617` | **`live_mode.md` 新規作成**（ライブ配信モードの運用手順、181 行） |
| `0d2d2ed` | **`design_change_record.md` 新規作成**（220 行）。旧仕様のアーカイブと読み替え表 |
| `0f11f14` / `38dae83` | 監査で見つかった陳腐化した記述を修正。`uv sync 厳禁` の適用範囲を明確化、AWS スケジュールを暫定と明記 |
| `86fda5e` | 「1 run = モデル 1 本」という現実に local_demo.md を合わせた |
| `1917f16` | 固定ステップ制・live_server 主役の現行設計へ docs 全体を同期（9 files / +360 -188） |
| `1135d3a` / `4abb5a4` / `b70d880` | 陳腐化したコマンド例・checkpoint 本数・ステージ範囲の記述を修正 |
| `84830f4` | 報酬設計の「不整合」項目を削除（現状で問題ないことを確認したため） |

**`design_change_record.md` の位置づけ**: 旧記述に出会ったときの読み替え表。
「なぜそうしたか」を残すことで、同じ検討をやり直さずに済ませる目的で作られている。

---

## 9. 現時点の未確定事項

以下はいずれも**暫定値**で、運用しながら調整する前提。docs にも暫定である旨を明記してある。

| 項目 | 暫定値 | 未確定の理由 |
|---|---|---|
| **AWS 稼働スケジュール** | プリセット生成: 月初 1 日 09:00 JST（5,000 steps ≒ 35 分）<br>学習配信: 平日 10-18 JST（月 176h、祝日除く） | 稼働時間帯・学習頻度・ステップ数のいずれも運用実績が無い。特に「平日 8h」はユーザー判断で**暫定と明言**されている |
| **プリセット生成用インスタンス** | c6a.4xlarge Spot | 5,000 steps を n_envs=1 で回すのは 1 コア × 約 35 分で、16 vCPU は明らかに過剰。小型化するか live EC2 の月初プリステップに畳み込む案がある |
| **live インスタンスの段階** | 推奨 c6a.2xlarge（最低 c6a.xlarge / 最高 m7a.2xlarge） | 3 段階を提示済みだが、実配信でのフレーム落ち具合を見て確定させる |
| **Fargate（learner のみ）** | EC2 のまま | live 側に ECS 化の動機が出たらセットで再検討（§5-5） |
| **`world_state` の永続化** | `/app/state` を mount のみ | live_server 側に読み書きの実装が無い。将来用の器だけある状態 |
| **Spot 中断からの復帰** | 手動 `start-instances` | ASG 撤去で自動再起動を意図的に手放した。頻度を見て自動化するか判断する |
| **月額コスト見積 約 ¥8,900** | 推奨構成前提 | 最大項目は Interface Endpoint の $22（43%）で 24/7 課金。Public Subnet 移動などの削減案は保留 |

> **実デプロイは未実施**。`deploy/` 一式と Dockerfile は静的検査のみで、
> AWS 上での動作確認は行っていない（docker も未インストールのためイメージビルドも未検証）。

---

## 関連

- 現行仕様の基準: [`../CLAUDE.md`](../CLAUDE.md)
- 設計書: [`block_stacker_design.md`](block_stacker_design.md)
- 旧仕様の読み替え: [`design_change_record.md`](design_change_record.md)
- ライブ配信モード: [`live_mode.md`](live_mode.md)
- ローカル試運転: [`local_demo.md`](local_demo.md)
- AWS デプロイ: [`aws_deployment.md`](aws_deployment.md)
