# Godot 4 Client (C# / .NET)

block-stacker のストリーミング配信を受けて、PyBullet 物理シムの状態を
リアルタイムに 3D 描画する Godot クライアント。**C# (.NET)** で実装。

## レイアウト

```
client/
├── project.godot                    # Godot プロジェクト設定（.NET 有効）
├── block-stacker-client.csproj      # .NET プロジェクトファイル
├── scenes/
│   └── main.tscn                    # Node3D ルート + Camera + Light + WsClient
├── scripts/
│   └── WsClient.cs                  # WebSocket + プロトコルデコーダ + 描画
├── .gitignore                       # bin/ obj/ 等の除外
└── README.md
```

## 必要環境

⚠️ **重要: Godot Engine の "C# / .NET" エディションが必要です**。
通常の Godot 4 (GDScript のみの版) では C# スクリプトが動きません。

1. **Godot Engine - .NET 4.4+**:
   - https://godotengine.org/download から、**".NET" 版**（または "Mono" 版）をダウンロード
   - 確認済み環境: `Godot_v4.4.1-stable_mono_win64` （Windows）
   - macOS/Linux/Windows いずれも C# 対応版がある

2. **.NET 8.0 SDK**:
   - https://dotnet.microsoft.com/download/dotnet/8.0 からインストール
   - `dotnet --version` で 8.0.x が表示されることを確認

3. **（任意）IDE**:
   - VS Code + C# Dev Kit 拡張、または
   - JetBrains Rider、または
   - Visual Studio Community

## セットアップ

1. **依存をリストア** （初回 + .csproj 編集後）:
   ```powershell
   cd client
   dotnet restore
   ```

2. **C# プロジェクトをビルド**（Godot エディタから自動でも可）:
   ```powershell
   dotnet build
   ```

3. **Godot エディタで開く**:
   - Godot Engine - .NET 版を起動
   - "Import" → `client/project.godot` を選択 → "Import & Edit"
   - 初回は自動で .NET アセンブリがビルドされる

4. **サーバを起動** （別ターミナルで）:
   ```powershell
   .venv\Scripts\python.exe -m block_stacker.serving.demo_server --port 8765
   ```

5. **クライアントを実行**:
   - Godot エディタで **F5**（または再生ボタン）
   - Godot ウィンドウが起動して `ws://localhost:8765` に接続
   - 4 形状（立方体・直方体・三角柱・円柱）が描画されるはず

## スクリプトの動作

`WsClient.cs` が以下を担当:

- **`_Ready`**: `ServerUri` (デフォルト `ws://localhost:8765`) に接続
- **`_Process`**: 受信パケットをドレインして type byte で dispatch
- **形状ごとに `MultiMeshInstance3D`**: 同じ形状のブロックは MultiMesh で
  バッチ描画（パフォーマンス向上）
- **`_lastRenderedTs`**: WebSocket は順序保証されないので、古い snapshot を drop

## 描画される形状

| サーバ側 type | クライアント側 Mesh | 備考 |
|------------|------------------|------|
| `box` | `BoxMesh` | 立方体・直方体共通 |
| `cylinder` | `CylinderMesh` | 円柱 |
| `triangular_prism` | カスタム `ArrayMesh` | 直角二等辺三角柱、サーバの頂点と一致 |
| その他 | フォールバック `BoxMesh` | warning ログを出す |

## カスタマイズ（Inspector で変更可能）

| プロパティ | デフォルト | 説明 |
|----------|---------|------|
| `ServerUri` | `ws://localhost:8765` | 接続先 WebSocket URL |
| `HelloPayload` | `{"client_version":"godot-csharp-v1"}` | 接続直後に送るハンドシェイク文字列 |
| `AutoReconnectSeconds` | `2.0` | 切断検知後の自動再接続待ち時間 |
| `ConnectingText` | `サーバとの通信を試行中` | 接続未確立時に画面中央に表示するメッセージ |

これらは `main.tscn` の World ノードを選択して Inspector パネルから編集できます。

## 接続状態の UI

サーバとの WebSocket が **OPEN 状態でないとき**、画面中央に以下のメッセージが
表示されます（0.5 秒ごとに「.」が増減するアニメーション付き）:

```
サーバとの通信を試行中...
```

接続が確立すると自動的に非表示になります。サーバが途中で落ちた場合も再表示されます。
プログラマティックに `CanvasLayer` + `Label` を生成しているので `main.tscn` 側の
編集は不要です。

## プロトコル

`src/block_stacker/streaming/protocol.py` と完全に対応。
すべて little-endian バイナリ、ポーズは float32、タイムスタンプは float64。

メッセージ種別:

| 0x | 名前 | 内容 |
|----|------|------|
| 01 | WORLD_CONFIG | 全ブロック静的情報（type, dims, color）の JSON |
| 02 | INITIAL_STATE | 全ブロックの初期ポーズ + awake フラグ |
| 03 | SNAPSHOT | AWAKE なブロックのポーズ更新（高頻度） |
| 04 | SLEEP_EVENT | ブロックが停止状態に入った通知 |
| 05 | WAKE_EVENT | ブロックが動き出した通知 |
| 07 | HEARTBEAT | 接続維持用 (現在は no-op) |
| 08 | COLLAPSE_EVENT | タワー崩落イベント（将来: 視覚効果のフック） |

## トラブルシューティング

| 症状 | 確認・対処 |
|------|---------|
| Godot エディタで "C# script support not enabled" 表示 | Godot の .NET 版を使っているか確認 |
| ビルドエラー "Godot.NET.Sdk not found" | `dotnet restore` 実行、または .NET 8 SDK のインストール |
| WebSocket 接続失敗 | サーバが起動しているか、`ServerUri` のポート番号が一致するか |
| 三角柱が見えない / 立方体に化ける | WsClient.cs が最新版か、エディタを再起動 |
| 形状が一切描画されない | サーバが WORLD_CONFIG を送る前に切断していないか、Output パネルの warning 確認 |

## 今後の拡張候補

- **崩落エフェクト**: `OnCollapseEvent` にパーティクルや画面フラッシュを追加
- **OSD**: タワー高さ / 現在ステージ / 学習時間を画面に表示
- **カメラ切替**: 軌道カメラ、AI 視点、トップダウン視点
- **サウンド**: ブロック配置音 / 崩落音

設計ドキュメント側の付録 F も参照。
