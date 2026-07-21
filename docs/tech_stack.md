# 使用技術一覧（Tech Stack）

積み木 AI 配信サービス「block-stacker」が使用している技術を、カテゴリ別にまとめた資料です。
各技術について「技術の説明（平易に）」「このプロジェクトでの用途」「採用理由」の 3 点を記載しています。

> バージョン情報は `pyproject.toml`・`infra-terraform/main.tf`・`client/block-stacker-client.csproj` 等から抽出しています。

---

## 目次

- [A. 機械学習 / 強化学習](#a-機械学習--強化学習)
- [B. 物理シミュレーション](#b-物理シミュレーション)
- [C. 通信 / ストリーミング](#c-通信--ストリーミング)
- [D. クライアント / 描画](#d-クライアント--描画)
- [E. インフラ / クラウド（AWS）](#e-インフラ--クラウドaws)
- [F. 開発ツール・共通ライブラリ](#f-開発ツール共通ライブラリ)

---

## A. 機械学習 / 強化学習

「AI がどうやって積み木を学ぶか」を支える技術群です。

### Python 3.12（CPython・python.org 版）

汎用プログラミング言語。機械学習・データ処理に最も広く使われる言語のひとつで、豊富なライブラリが揃っている。

- **用途**: バックエンド全体の実装言語。学習（`training/`）・物理シム接続（`sim/`）・配信サーバ（`serving/`）・AWS Lambda すべてに使用。
- **採用理由**: PyTorch・Stable-Baselines3・PyBullet が Python を主要 API として持つため実質必須。バージョン 3.12 は PyBullet の numpy2 対応ソースビルド時の要件と合致する。`uv` 管理ではなく python.org 製の素の CPython を使うのは、`pybullet` が numpy2 ABI のパッケージを uv 環境では正しくビルドできないため。

### PyTorch（≥ 2.0）

ニューラルネットワーク（人工知能のモデル）を構築・学習・実行するためのライブラリ。Meta（旧 Facebook）開発。数値演算を CPU でも GPU でも効率的に処理できる。

- **用途**: `policy/` モジュール全体の NN 実装基盤。
  - `HybridFeatureExtractor`（`policy/feature_extractor.py`）: 観測辞書（ブロック位置・ハイトマップ・短期記憶）を 1 本の特徴ベクトルへ変換する「脳への入力処理」
  - `SetEncoder`（`policy/set_transformer.py`）: ブロック集合を順序不変に処理する自己注意機構（SAB; Lee et al. 2019 準拠）
  - `HeightmapCNN`（`policy/heightmap_cnn.py`）: 4ch × 32×32 のタワー形状マップを CNN（Conv 3 段 + AdaptiveAvgPool）で圧縮
- **採用理由**: Stable-Baselines3 が PyTorch バックエンドを前提とするため実質必須。`nn.Module` の自由なサブクラス化によって、カスタム NN モジュールを SB3 のポリシーに自然に組み込める。

### Stable-Baselines3（≥ 2.3.0）

強化学習アルゴリズムの実装集。「エージェントが試行錯誤しながら報酬を最大化するように学習する」ための仕組みを提供する、実績のある Python ライブラリ。

- **用途**: SAC（Soft Actor-Critic）アルゴリズムの実装基盤。`training/train.py` が `SAC` クラスを使い、`MultiInputPolicy` と `HybridFeatureExtractor` を組み合わせて学習を実行。`CheckpointCallback`（定期保存）・`StageMonitorCallback`（カリキュラム指標の記録）も SB3 の Callback API を継承して実装。学習 env は `Monitor` で包む（報酬曲線の記録に必須）。
- **採用理由**: SAC は連続行動空間（アームの動かし方が無段階）に強く、物理シムで微妙な動きを学ぶのに適している。SB3 は SAC の安定した参照実装を提供しており、`MultiInputPolicy` により Dict 型の複雑な観測空間（ブロック位置・ハイトマップなどの複数情報を同時に渡す形式）をそのまま扱える点が決め手。

### Gymnasium（≥ 0.29）

強化学習の「環境インターフェース」を定義する標準ライブラリ（旧 OpenAI Gym の後継）。エージェントと環境の通信規約（`reset()` で初期化、`step(action)` → `(観測, 報酬, 終了フラグ, 情報)` を返す）を規定する。

- **用途**: `env/env.py` の `BlockStackerEnv` が `gymnasium.Env` を継承。複数環境を並列実行する `SubprocVecEnv`（SB3 付属）もこのインターフェースを前提とする。
- **採用理由**: SB3 が Gymnasium 準拠の環境を前提とするため必須。

### TensorBoard（≥ 2.14）

機械学習の学習ログをブラウザでグラフ表示するツール。損失・成功率・ステップ数の推移をリアルタイムに可視化できる。

- **用途**: `SAC.learn()` が自動的に学習メトリクス（`actor_loss`・`critic_loss`・`success_rate`・現在 `stage` 等）を `output/training/tb/` 以下に記録。開発中のデバッグ・ハイパーパラメータ調整に利用。
- **採用理由**: SB3 が標準で TensorBoard 出力に対応しており、追加実装なしで学習状態を可視化できるため。

### WeightedReplayBuffer（カスタム実装）

標準の SAC が使うリプレイバッファ（過去の経験を貯蓄するメモリ）に、人間の記憶メカニズムを模した拡張を加えたカスタムクラス（`policy/weighted_replay_buffer.py`）。

SB3 の `DictReplayBuffer` を継承し、以下 4 つの機能を追加している。

| 機能 | 内容 |
|---|---|
| 重要度の差 | イベント種別ごとに初期重みを変える（崩落 > 失敗 > 成功 > 無駄手） |
| 時間減衰 | 各記憶の重みを 1 ステップごとに一定率で減衰（古い記憶ほど薄れる） |
| 重みつきサンプリング | 重みに比例した確率で記憶を引く（重要な経験ほど学習に多く使われる） |
| 想起ノイズ | 重みに反比例したノイズを行動に加える（弱い記憶ほど曖昧に再現される） |

- **採用理由**: 「面白い経験（崩落・初成功）をより多く学習に活用する」ことで学習効率を上げるとともに、プロジェクトのコンセプト（「子供が経験から学ぶ記憶の仕組み」）を実装に反映させるため。

### オートカリキュラム（カスタム実装）

難易度を段階的に上げていく学習スケジューリングの仕組み（`training/curriculum.py`）。最初は簡単なステージで基礎を学ばせ、決められたステップ数を消化したら次の難易度へ進む（**固定ステップ制**。卒業判定は行わない）。

- **用途**: Stage 1（cube のみ）→ Stage 2（cube 増量）→ Stage 3（cuboid 追加）→ Stage 4（三角柱追加）→ Stage 5（円柱追加・全形状）の 5 段階。各ステージの予算は `stages[].steps`（既定 60k/35k/40k/45k/70k の U 字配分）で、`--stage-steps` により一括／ステージ別に上書きできる。`StageMonitorCallback` は success_rate と all_placed を記録するのみで進行には影響しない。
- **採用理由**: ランダム初期化から最終ステージを一気に学習させると難しすぎて学習が収束しない。カリキュラムにより段階的に難度を上げることで、より安定した学習が実現できる。

---

## B. 物理シミュレーション

「積み木が現実のように動く」を支える技術です。

### PyBullet（≥ 3.2.6・numpy2 対応ソースビルド版）

オープンソースの剛体物理シミュレーションエンジン。物体に重力・摩擦・衝突を適用し、現実に近い動きを計算する。ゲームや研究用ロボットシミュレーションに広く使われる。

- **用途**: `sim/` モジュール全体。ブロックの生成（`sim/blocks.py`：直方体・円柱・三角柱）・地面と境界壁（`sim/world.py`）・キャリア（把持アーム、`sim/carrier.py`）・ハイトマップ計算（`sim/heightmap.py`）をすべて PyBullet 上で実装。物理演算は 240Hz で実行し、ブロックを置いた後の静定待ち（settle フェーズ）を含む。
- **採用理由**: Python から直接 API を呼べる数少ない高品質な物理エンジン。GPU 不要で CPU だけで動作するため Spot インスタンス（CPU 特化型）での学習に適している。ただし PyPI 配布ホイールは numpy 1 ABI のみで numpy 2 系と互換がないため、ソースからビルドした版が必須。

---

## C. 通信 / ストリーミング

「学習結果をリアルタイムに Godot クライアントへ届ける」を支える技術です。

### websockets（≥ 12.0）

Python で WebSocket サーバを実装するためのライブラリ。WebSocket は HTTP の上に乗る双方向リアルタイム通信の規格で、サーバからクライアントへデータを「プッシュ」し続けられる。

- **用途**: `streaming/server.py` が asyncio ベースの WebSocket サーバを実装。`PhysicsBroadcaster`（`streaming/broadcaster.py`）が 240Hz の物理ループから得たブロック姿勢データを各 Godot クライアントへ配信。
- **採用理由**: asyncio ネイティブで軽量、Python との親和性が高い。学習スレッドと配信ループを同一プロセスで共存させる live_server の構成と相性が良い。

### asyncio（Python 標準ライブラリ）

Python 標準の非同期処理フレームワーク。複数の I/O 処理（WebSocket 送信・物理ループ・AI 推論）を 1 スレッドで効率よく並行実行するための仕組み。

- **用途**: `serving/live_server.py` のメインループ。物理シム（`PhysicsBroadcaster`）・AI ドライバ（`ai_driver_task`）・WebSocket 配信を別 asyncio タスクとして並行実行。学習スレッド（`threading.Thread`）との協調も asyncio イベントループ経由で行う。
- **採用理由**: I/O 待ち（WebSocket 送信）が多い配信系タスクを、マルチスレッドよりシンプルかつ安全に並行化できるため。

### 独自バイナリプロトコル（`streaming/protocol.py`）

フレームの先頭 1 バイトでメッセージ種別を区別するカスタム設計の通信プロトコル。サーバ（Python）とクライアント（C#）で対称的に実装されている。

8 種のメッセージ型を定義している。

| バイト値 | メッセージ型 | 内容 |
|---|---|---|
| `0x01` | WORLD_CONFIG | ブロック形状・寸法・地面サイズの初期設定（JSON） |
| `0x02` | INITIAL_STATE | 全ブロックの初期姿勢（一括送信） |
| `0x03` | SNAPSHOT | 毎フレーム：起動中ブロックの姿勢（位置 xyz + 四元数 xyzw）をバイナリで送信 |
| `0x04` | SLEEP_EVENT | ブロックが静止した通知 |
| `0x05` | WAKE_EVENT | ブロックが動き出した通知 |
| `0x07` | HEARTBEAT | 接続維持用の定期送信 |
| `0x08` | COLLAPSE_EVENT | タワー崩落の通知 |

- **採用理由**: JSON テキストでは 240Hz の高頻度配信に対してデータ量が大きすぎる。コンパクトなリトルエンディアン float のバイナリ形式を採用することで帯域を削減しつつ、クライアント側（C# の `StreamPeerBuffer`）でも容易にデコードできる構造にしている。

---

## D. クライアント / 描画

「積み木が積まれていく様子を 3D で見る」を支える技術です。

### Godot Engine 4.4.1（Mono 版）

オープンソースの 3D/2D ゲームエンジン。ゲームのような 3D シーンをリアルタイムに描画できる。Mono 版は C# スクリプトが使えるエディション。

- **用途**: 配信クライアント（`client/`）全体。WebSocket でブロック姿勢を受信し、3D シーン上でリアルタイムに積み木を描画する。`MultiMeshInstance3D` を使って同一形状のブロックをまとめてバッチ描画。光源・影の設定も調整済み（`directional_shadow_max_distance=6` 等、cm 級スケール向け）。
- **採用理由**: リアルタイム 3D 描画に最適化されており、WebSocket クライアント（`WebSocketPeer`）も標準で備える。無料・オープンソースかつ C# が使えるため型安全なクライアントコードを書ける。

### C# / .NET 8.0

Microsoft が開発した型付きプログラミング言語とそのランタイム。Godot Mono 版の公式スクリプト言語。

- **用途**: `client/scripts/WsClient.cs`。WebSocket 受信・バイナリデコード・座標変換（PyBullet の Z-up 座標系 → Godot の Y-up 座標系、X 軸まわり −90° 回転）・`MultiMesh` への姿勢書き込み・自動再接続ロジックをすべて C# で実装。三角柱の `ArrayMesh`（8 三角形フェイス、per-face normal で flat shading）も手組みしている。
- **採用理由**: Godot Mono 版のスクリプト言語として自然な選択。GDScript より型安全性が高く、バイナリプロトコルの低レベルなバイト操作も書きやすい。

### MultiMeshInstance3D（Godot 組み込み機能）

同一メッシュの複数インスタンスを 1 回の描画コール（GPU インスタンシング）でまとめて描く Godot の仕組み。

- **用途**: ブロックを形状（box / cylinder / triangular_prism）ごとに `MultiMesh` にまとめて描画。cube が 10 個あっても個別に 10 回描くのではなく 1 回の描画コールで済む。
- **採用理由**: ブロック数が増えても描画負荷がほぼ増加しない。CPU-GPU 間のデータ転送を最小化し、240Hz 配信に追随できるフレームレートを実現するため。

---

## E. インフラ / クラウド（AWS）

「AWS 上でサービスを自動運用する」を支える技術です。

### Terraform（≥ 1.6 / HashiCorp AWS provider ≈ 5.0）

クラウドインフラをコードで記述・管理するツール（Infrastructure as Code）。AWS のサーバや設定を `*.tf` ファイルに書いておけば `terraform apply` 一発で再現できる。

- **用途**: `infra-terraform/` 以下に全 AWS リソースを定義。VPC・サブネット・EC2・ASG・Lambda・EventBridge Scheduler・S3・IAM・CloudWatch・EIP をすべて Terraform で管理。State は S3 + DynamoDB で管理し、複数人の同時変更による競合を防いでいる。
- **採用理由**: インフラの構成をコードとして Git で管理できるため、環境の再現性・変更履歴が保証される。手作業による設定ミスを排除できる点も重要。

### AWS EC2 Spot インスタンス

AWS が提供する仮想サーバ（EC2）。Spot インスタンスは AWS の余剰キャパシティを安価に借りる形式で、通常料金より 60〜80% 安く利用できる（需要逼迫時に強制中断される可能性と引き換え）。

| ロール | インスタンス種別 | vCPU / アーキ | 用途 |
|---|---|---|---|
| 配信（streamer） | t4g.small | 2 vCPU / ARM | WebSocket 配信サーバ |
| デモ（demo） | c6i.xlarge | 4 vCPU / Intel | AI 推論 + 配信 |
| 学習（learner） | c6a.4xlarge | 16 vCPU / AMD EPYC | SAC バックグラウンド学習 |

- **採用理由**: NN のサイズが小さく PyBullet が CPU バウンドであるため GPU インスタンス（g4dn 等）は過剰投資と判断。AMD EPYC CPU（c6a）は g4dn 比で約 40% コスト削減。ARM（t4g）は配信サーバの軽い CPU 負荷に合わせたコスト最適化。

### AWS Lambda（Python 3.12）

「サーバを常時稼働させずに、呼ばれた時だけコードを実行する」サーバレス関数サービス。

- **用途**: `lambda/handler.py`。EventBridge Scheduler から定時に呼ばれ、EC2 Auto Scaling Group の `desired_capacity` を 0/1 に変更してインスタンスの起動・停止を制御。日本の祝日判定（`jpholiday` ライブラリ）も内蔵し、祝日はスケールアップをスキップする。
- **採用理由**: 起動・停止のトリガーだけなら常時稼働サーバは不要。Lambda の方が安価かつシンプルに実現できる。

### AWS EventBridge Scheduler

時刻を指定して Lambda などを自動実行するサービス。cron 式でスケジュールを記述できる。

- **用途**: 学習 EC2（隔週土曜 14-22 JST）とデモ・配信 EC2（平日 14-22 JST）の自動起動・停止スケジュールを管理（`infra-terraform/scheduler.tf`）。Lambda 1 ペアを複数スケジュールで共有し、payload の `asg_names` で対象 ASG を切り替える設計。
- **採用理由**: cron 式でスケジュールを Terraform コードとして管理でき、変更・レビューが容易。

### AWS Auto Scaling Group（ASG）

EC2 インスタンスの台数を自動管理するサービス。`desired_capacity` を変えるだけで起動・停止が自動化される。

- **用途**: streamer / demo / learner の 3 ASG を定義（`infra-terraform/ec2.tf`）。平常時は `desired_capacity=0` で停止、稼働時間中は Lambda が `1` に変更。`capacity-optimized` 戦略で Spot 強制中断を最小化。
- **採用理由**: 起動・停止を ASG に委ねることで、インスタンス障害時の自動復旧（`health_check_type = "EC2"`）も同時に実現できる。

### AWS S3（Simple Storage Service）

AWS のオブジェクトストレージ。ファイルをシンプルかつ安価に保存・取得できる。

- **用途**: ① 学習済みモデル（`.zip`）・リプレイバッファ（`.pkl`）の永続保存先。② Terraform の State ファイル保存。③ Lambda デプロイパッケージの置き場。
- **採用理由**: EC2 インスタンスは起動・停止を繰り返すため、永続データはインスタンス外に置く必要がある。S3 は高耐久（イレブンナイン）で安価。

### AWS CloudWatch

AWS のログ収集・メトリクス監視・アラートサービス。EC2・Lambda のログをまとめて管理できる。

- **用途**: 3 EC2 ロールと Lambda 2 関数のログを CloudWatch Logs に保存（`infra-terraform/cloudwatch.tf`）。Lambda エラーや ASG 正常性の低下を SNS（メール通知）経由でアラート。
- **採用理由**: AWS サービスとの統合が容易で、追加実装なしでログ収集とアラートが実現できるため。

### Amazon Linux 2023

AWS が提供する Linux OS イメージ（AMI）。

- **用途**: EC2 インスタンスの OS として使用。ARM（t4g.small 用）と x86_64（c6i / c6a 用）の両 AMI を使用。
- **採用理由**: AWS 公式サポートで脆弱性対応が速く、EC2 環境との親和性が高い。

---

## F. 開発ツール・共通ライブラリ

### NumPy（≥ 1.26）

Python での数値計算（行列演算・配列処理）の標準ライブラリ。

- **用途**: `sim/heightmap.py`（ハイトマップ生成）・観測ベクトルのパッキング・報酬計算など、数値配列を扱うあらゆる箇所で使用。
- **採用理由**: PyBullet・Gymnasium・PyTorch のすべてが NumPy 配列を受け渡しの基本型として使うため実質必須。バージョン ≥ 1.26 は PyBullet numpy2 対応ビルドとの互換を保つために下限を設定。

### PyYAML（≥ 6.0）

Python で YAML ファイルを読み書きするライブラリ。YAML は人間が読み書きしやすい設定ファイル形式。

- **用途**: `configs/` 以下の 4 設定ファイル（`world.yaml` / `physics.yaml` / `reward.yaml` / `training.yaml`）を `config.py` が読み込む際に使用。物理パラメータ・報酬設計・ハイパーパラメータをすべてコードから分離して管理。
- **採用理由**: コードを変更せずに設定を調整できるため、実験のサイクルが速くなる。

### Ruff（≥ 0.4）

Python の超高速 Linter（コード品質チェックツール）。スタイル違反・未使用インポート・型ヒントの古い書き方などを自動検出する。

- **用途**: `src/` 以下のコードチェック。`pyproject.toml` で `select = ["E", "F", "I", "B", "UP"]` を設定。CI や手動で `.venv\Scripts\python.exe -m ruff check src/ --select E,F,I,B,UP` として実行。
- **採用理由**: flake8 + isort + pyupgrade を 1 ツールで置き換えられ、Rust 実装で非常に高速。

### mypy（≥ 1.8）

Python の静的型チェッカー。型ヒントを検証し、型の不整合をコード実行前に発見できる。

- **用途**: 開発時の型安全性確認。`pyproject.toml` で `no_implicit_optional = true` 等を設定し、暗黙の `None` 許容を防止。
- **採用理由**: 複数モジュールが連携する大きなコードベースで、型ミスを早期に発見するため。

### pytest（≥ 7.0）

Python のテストフレームワーク。テストを書いて `pytest -q` コマンドで一括実行できる。

- **用途**: `tests/` 以下。7 ファイル・計 96 テストを管理（`test_checkpoint.py` 20、`test_env_basic.py` 8、`test_policy.py` 7、`test_sim_basic.py` 6、`test_streaming.py` 8、`test_train.py` 13、`test_live_server.py` 34）。
- **採用理由**: Python のデファクトスタンダードのテストフレームワーク。`pyproject.toml` の `testpaths = ["tests"]` 設定で最小構成で動く。

### Hatchling

Python パッケージのビルドシステム（`pyproject.toml` の `[build-system]` で指定）。

- **用途**: `pip install -e .` 時に `src/block_stacker/` を `block_stacker` パッケージとして認識させる。
- **採用理由**: PEP 517/518 標準に準拠したモダンなビルドバックエンドとして採用。

### PowerShell（Windows）

Windows 標準の高機能シェルスクリプト環境。

- **用途**: `tools/` 以下の運用スクリプト群。`advance_day.ps1`（日次モデル切り替え）・`local_loop.ps1`（ローカル 1 巡再生）・`demo_checkpoints.ps1`（手動デモ）・`deploy/` 以下のデプロイ手順スクリプトを PowerShell で実装。
- **採用理由**: 開発環境が Windows であるため。AWS CLI・Terraform との連携も PowerShell スクリプトで統一している。

### boto3

Python から AWS の各サービスを操作するための公式 SDK。

- **用途**: `lambda/handler.py` 内で `boto3.client("autoscaling")` として使用し、ASG の `desired_capacity` を変更。
- **採用理由**: AWS Lambda（Python ランタイム）では boto3 が標準で使えるため、追加インストール不要。

### jpholiday

日本の祝日を判定する Python ライブラリ。

- **用途**: `lambda/handler.py` の `scale_up` 関数内で使用。祝日は学習・デモの起動をスキップするロジックに使用。
- **採用理由**: 日本市場向けのサービスのため祝日スキップが必要。軽量な pure-Python ライブラリで Lambda への同梱が容易。

---

## 関連ドキュメント

- [`docs/block_stacker_design.md`](block_stacker_design.md) — アーキテクチャ設計書（各コンポーネントの詳細設計）
- [`docs/local_demo.md`](local_demo.md) — ローカル環境での試運転手順
- [`docs/live_mode.md`](live_mode.md) — ライブ配信モード（`live_server.py`）手順
- [`docs/aws_deployment.md`](aws_deployment.md) — AWS デプロイ手順
- [`docs/log_reading.md`](log_reading.md) — ログ解読・単語帳
