using Godot;
using System;
using System.Collections.Generic;
using GArray = Godot.Collections.Array;
using GDictionary = Godot.Collections.Dictionary;

/// <summary>
/// WebSocket client for the block-stacker streaming server (MVP 3 protocol).
///
/// Connects to ws://HOST:PORT, decodes binary messages, and updates a
/// MultiMeshInstance3D per shape with the latest block poses. Renders only
/// AWAKE blocks per-frame via the snapshot stream; sleep/wake events flip
/// blocks in/out of the active set.
///
/// 設計上のポイント（日本語レビューノート）:
///   - 4 形状（box / cylinder / triangular_prism / fallback）の Mesh をクライアント側で
///     構築する。三角柱はサーバ側（sim/blocks.py）と同じ頂点レイアウトで ArrayMesh を
///     生成し、見た目と物理シムの形状を一致させる。
///   - MultiMeshInstance3D を「形状名ごと」に生成。同一形状のブロックは 1 つの
///     MultiMesh インスタンスとしてバッチ描画。
///   - last_rendered_ts で out-of-order frame を drop。WebSocket は順序保証されないので必須。
///   - 自動再接続: ServerUri が落ちている時は AutoReconnectSeconds ごとに再試行。
/// </summary>
public partial class WsClient : Node3D
{
    // localhost にすると Windows で IPv6 (::1) を先に試して timeout 待ちになることがあるため、
    // 明示的に IPv4 アドレスを既定値に。本番デプロイ時は Inspector から書き換える。
    [Export] public string ServerUri = "ws://127.0.0.1:8765";
    [Export] public string HelloPayload = "{\"client_version\":\"godot-csharp-mvp3\"}";
    [Export] public float AutoReconnectSeconds = 2.0f;
    [Export] public string ConnectingText = "サーバとの通信を試行中";

    // Message type bytes (mirror src/block_stacker/streaming/protocol.py)
    private const byte MSG_WORLD_CONFIG   = 0x01;
    private const byte MSG_INITIAL_STATE  = 0x02;
    private const byte MSG_SNAPSHOT       = 0x03;
    private const byte MSG_SLEEP_EVENT    = 0x04;
    private const byte MSG_WAKE_EVENT     = 0x05;
    private const byte MSG_HEARTBEAT      = 0x07;
    private const byte MSG_COLLAPSE_EVENT = 0x08;

    // 地面 MeshInstance3D（world_config 受信時に再構築）
    private MeshInstance3D? _groundMesh;

    // Per-block static info: id -> { shape, type, dims, color }
    private readonly Dictionary<int, GDictionary> _blockInfo = new();
    // Per-block runtime pose: id -> Transform3D
    private readonly Dictionary<int, Transform3D> _poses = new();
    // Set of awake block ids (only these get updated per snapshot)
    private readonly HashSet<int> _awake = new();
    // Per-shape MultiMeshInstance3D
    private readonly Dictionary<string, MultiMeshInstance3D> _multimeshes = new();
    // block_id -> int (index within its shape's MultiMesh)
    private readonly Dictionary<int, int> _instanceIndex = new();

    private WebSocketPeer? _socket;
    private double _lastRenderedTs = -1.0;
    private int _droppedDueToOrdering = 0;
    private double _reconnectTimer = 0.0;
    private bool _helloSent = false;

    // 接続状態の UI（_Ready で必ず初期化されるため null! で warning 抑制）
    private CanvasLayer _uiLayer = null!;
    private Label _statusLabel = null!;
    private double _statusAnimTime = 0.0;

    public override void _Ready()
    {
        SetupUi();
        Connect();
    }

    private void SetupUi()
    {
        _uiLayer = new CanvasLayer { Layer = 1 };
        AddChild(_uiLayer);

        _statusLabel = new Label
        {
            Text = ConnectingText,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        // 画面全体をカバーするアンカー → テキストが中央に表示される
        _statusLabel.SetAnchorsPreset(Control.LayoutPreset.FullRect);

        // 視認性: 大きめのフォント + 白文字 + 黒アウトライン
        _statusLabel.AddThemeFontSizeOverride("font_size", 36);
        _statusLabel.AddThemeColorOverride("font_color", new Color(1f, 1f, 1f, 0.95f));
        _statusLabel.AddThemeColorOverride("font_outline_color", new Color(0f, 0f, 0f, 0.85f));
        _statusLabel.AddThemeConstantOverride("outline_size", 6);

        _uiLayer.AddChild(_statusLabel);
    }

    private void UpdateStatusUi(double delta)
    {
        bool connected = _socket != null
            && _socket.GetReadyState() == WebSocketPeer.State.Open;
        if (connected)
        {
            _statusLabel.Visible = false;
            _statusAnimTime = 0.0;
            return;
        }
        _statusLabel.Visible = true;
        // 「...」のドットアニメーション（0.5 秒ごとに dot 数が 0→1→2→3→0 で循環）
        _statusAnimTime += delta;
        int dots = ((int)(_statusAnimTime * 2.0)) % 4;
        _statusLabel.Text = ConnectingText + new string('.', dots);
    }

    public override void _Process(double delta)
    {
        if (_socket == null)
        {
            _reconnectTimer -= delta;
            if (_reconnectTimer <= 0.0)
                Connect();
            UpdateStatusUi(delta);
            return;
        }

        _socket.Poll();
        var state = _socket.GetReadyState();
        if (state == WebSocketPeer.State.Open)
        {
            // 初回 Open 到達時に hello を送る（接続前の SendText は Godot 4.4 でエラーになる）。
            if (!_helloSent && !string.IsNullOrEmpty(HelloPayload))
            {
                _socket.SendText(HelloPayload);
                _helloSent = true;
            }
            while (_socket.GetAvailablePacketCount() > 0)
            {
                byte[] packet = _socket.GetPacket();
                HandleMessage(packet);
            }
        }
        else if (state == WebSocketPeer.State.Closed)
        {
            int code = _socket.GetCloseCode();
            GD.PushWarning($"ws closed (code={code}), reconnecting in {AutoReconnectSeconds:F1}s");
            _socket = null;
            _reconnectTimer = AutoReconnectSeconds;
        }
        UpdateStatusUi(delta);
    }

    private void Connect()
    {
        _socket = new WebSocketPeer();
        _helloSent = false;
        var err = _socket.ConnectToUrl(ServerUri);
        if (err != Error.Ok)
        {
            GD.PushWarning($"connect failed: {err}");
            _socket = null;
            _reconnectTimer = AutoReconnectSeconds;
            return;
        }
        // hello は STATE_OPEN 到達後に送る。Godot 4.4 の C# binding では
        // CONNECTING 状態での SendText は FAILED エラーが出るため、_Process 側で遅延送信。
    }

    // ---- Dispatch ----------------------------------------------------------

    private void HandleMessage(byte[] data)
    {
        if (data.Length == 0) return;
        byte msgType = data[0];
        switch (msgType)
        {
            case MSG_WORLD_CONFIG:   OnWorldConfig(data); break;
            case MSG_INITIAL_STATE:  OnInitialState(data); break;
            case MSG_SNAPSHOT:       OnSnapshot(data); break;
            case MSG_SLEEP_EVENT:    OnSleepEvent(data); break;
            case MSG_WAKE_EVENT:     OnWakeEvent(data); break;
            case MSG_HEARTBEAT:      break;
            case MSG_COLLAPSE_EVENT: OnCollapseEvent(data); break;
            default:
                GD.PushWarning($"unknown msg type 0x{msgType:X2}");
                break;
        }
    }

    // ---- Decoders ----------------------------------------------------------

    private static StreamPeerBuffer ReadBuf(byte[] data)
    {
        var buf = new StreamPeerBuffer { DataArray = data, BigEndian = false };
        return buf;
    }

    private void OnWorldConfig(byte[] data)
    {
        // Layout: [type:1][json_len:4][json:json_len]
        uint jsonLen = BitConverter.ToUInt32(data, 1);
        string jsonStr = System.Text.Encoding.UTF8.GetString(data, 5, (int)jsonLen);
        var parsed = Json.ParseString(jsonStr);
        if (parsed.VariantType != Variant.Type.Dictionary)
        {
            GD.PushWarning("world_config JSON parse failed");
            return;
        }
        var cfg = parsed.AsGodotDictionary();

        _blockInfo.Clear();
        _instanceIndex.Clear();
        _poses.Clear();
        _awake.Clear();

        if (cfg.ContainsKey("blocks"))
        {
            var blocksRaw = cfg["blocks"].AsGodotArray();
            foreach (var raw in blocksRaw)
            {
                var info = raw.AsGodotDictionary();
                int bid = info["id"].AsInt32();
                _blockInfo[bid] = info;
            }
        }
        RebuildGround(cfg);
        RebuildMultimeshes();
    }

    /// <summary>
    /// world_config の "ground" フィールドから地面のサイズを読み取って描画する。
    /// 設計上のポイント:
    ///   - サーバ側 sim/world.py の _spawn_ground と同じ寸法: size [x, y] × 厚み 0.02m
    ///   - サーバの座標系 (Z-up) → Godot (Y-up) の差は現状未補正のため、地面は
    ///     Godot 標準の XZ 平面（Y=0）に置く。位置は厚みの半分だけ下げて、上面が Y=0 に。
    ///   - 灰色（PyBullet 側の色と一致）、shadow キャッチャー有効。
    /// </summary>
    private void RebuildGround(GDictionary cfg)
    {
        if (_groundMesh != null)
        {
            _groundMesh.QueueFree();
            _groundMesh = null;
        }
        if (!cfg.ContainsKey("ground")) return;

        var groundDict = cfg["ground"].AsGodotDictionary();
        if (!groundDict.ContainsKey("size")) return;
        var sizeArr = groundDict["size"].AsGodotArray();
        float sx = sizeArr.Count > 0 ? sizeArr[0].AsSingle() : 3.0f;
        float sz = sizeArr.Count > 1 ? sizeArr[1].AsSingle() : 3.0f;

        const float thickness = 0.02f;
        var groundBox = new BoxMesh { Size = new Vector3(sx, thickness, sz) };

        var material = new StandardMaterial3D
        {
            AlbedoColor = new Color(0.4f, 0.4f, 0.4f, 1.0f),
            Roughness = 0.9f,
            Metallic = 0.0f,
        };

        _groundMesh = new MeshInstance3D
        {
            Name = "Ground",
            Mesh = groundBox,
            // 上面が Y=0 になるよう、厚みの半分だけ下げる。
            Position = new Vector3(0f, -thickness / 2f, 0f),
            MaterialOverride = material,
        };
        AddChild(_groundMesh);
    }

    private void OnInitialState(byte[] data)
    {
        var buf = ReadBuf(data);
        buf.GetU8();
        double ts = buf.GetDouble();
        ushort n = (ushort)buf.GetU16();
        for (int i = 0; i < n; i++)
        {
            int bid = buf.GetU16();
            byte awakeFlag = (byte)buf.GetU8();
            var t = ReadPoseTransform(buf);
            _poses[bid] = t;
            if (awakeFlag != 0) _awake.Add(bid);
            else _awake.Remove(bid);
            SetInstanceTransform(bid, t);
        }
        _lastRenderedTs = ts;
    }

    private void OnSnapshot(byte[] data)
    {
        var buf = ReadBuf(data);
        buf.GetU8();
        double ts = buf.GetDouble();
        buf.GetU32();  // seq (unused for now)
        if (ts <= _lastRenderedTs)
        {
            _droppedDueToOrdering++;
            return;
        }
        byte n = (byte)buf.GetU8();
        for (int i = 0; i < n; i++)
        {
            int bid = buf.GetU16();
            var t = ReadPoseTransform(buf);
            _poses[bid] = t;
            _awake.Add(bid);
            SetInstanceTransform(bid, t);
        }
        _lastRenderedTs = ts;
    }

    private void OnSleepEvent(byte[] data)
    {
        var buf = ReadBuf(data);
        buf.GetU8();
        buf.GetDouble();  // ts
        int bid = buf.GetU16();
        var t = ReadPoseTransform(buf);
        _poses[bid] = t;
        _awake.Remove(bid);
        SetInstanceTransform(bid, t);
    }

    private void OnWakeEvent(byte[] data)
    {
        var buf = ReadBuf(data);
        buf.GetU8();
        buf.GetDouble();
        int bid = buf.GetU16();
        _awake.Add(bid);
    }

    private void OnCollapseEvent(byte[] data)
    {
        // Hook for visual effects. 将来: パーティクル / 画面フラッシュ等。
    }

    // PyBullet (Z-up) → Godot (Y-up) 変換: X 軸まわり -90° 回転。
    // - 位置: (x, y, z) → (x, z, -y)
    // - 姿勢: q_godot = m * q_pybullet （左掛けで OK; 詳細は ReadPoseTransform のコメント参照）
    private static readonly Quaternion _zupToYup =
        new(new Vector3(1, 0, 0), -Mathf.Pi / 2);

    private static Transform3D ReadPoseTransform(StreamPeerBuffer buf)
    {
        float px = buf.GetFloat();
        float py = buf.GetFloat();
        float pz = buf.GetFloat();
        float qx = buf.GetFloat();
        float qy = buf.GetFloat();
        float qz = buf.GetFloat();
        float qw = buf.GetFloat();

        // 物理シム (PyBullet) は Z-up なので、Godot の Y-up に変換する。
        // 位置の変換は R * v に相当（X 軸まわり -90°）: (x, y, z) → (x, z, -y)。
        var position = new Vector3(px, pz, -py);
        // 姿勢の変換は q_godot = m * q_pybullet。物体のローカル軸 e に対して、
        //   PyBullet world での向き = q_pybullet * e
        //   Godot world での向き    = m * (q_pybullet * e) = (m * q_pybullet) * e
        // つまり左から m を掛けるだけで OK（似変換の必要なし）。
        var qPybullet = new Quaternion(qx, qy, qz, qw);
        var qGodot = _zupToYup * qPybullet;

        return new Transform3D(new Basis(qGodot), position);
    }

    // ---- Rendering ---------------------------------------------------------

    private void RebuildMultimeshes()
    {
        // Tear down existing MultiMesh nodes.
        foreach (var node in _multimeshes.Values)
            node.QueueFree();
        _multimeshes.Clear();
        _instanceIndex.Clear();

        // Group blocks by shape name.
        var perShape = new Dictionary<string, List<int>>();
        foreach (var bid in _blockInfo.Keys)
        {
            var info = _blockInfo[bid];
            string shapeName = info["shape"].AsString();
            if (!perShape.ContainsKey(shapeName))
                perShape[shapeName] = new List<int>();
            perShape[shapeName].Add(bid);
        }

        foreach (var (shapeName, ids) in perShape)
        {
            var sample = _blockInfo[ids[0]];
            Mesh mesh = BuildMesh(sample);
            Color color = ArrayToColor(sample.ContainsKey("color")
                ? sample["color"].AsGodotArray()
                : new GArray());

            // shape ごとに同じ色なので、MaterialOverride で MultiMesh 全体に色を当てる。
            // （MultiMesh.UseColors + SetInstanceColor を使う場合は material 側で
            //  vertex_color_use_as_albedo を有効にする必要があり、ややこしいので
            //  色固定ならこのアプローチが手軽。）
            var material = new StandardMaterial3D
            {
                AlbedoColor = color,
                Roughness = 0.6f,
                Metallic = 0.0f,
            };

            var mm = new MultiMesh
            {
                TransformFormat = MultiMesh.TransformFormatEnum.Transform3D,
                Mesh = mesh,
                InstanceCount = ids.Count,
            };

            var mmi = new MultiMeshInstance3D
            {
                Multimesh = mm,
                Name = "MM_" + shapeName,
                MaterialOverride = material,
            };
            AddChild(mmi);

            for (int i = 0; i < ids.Count; i++)
            {
                int bid = ids[i];
                _instanceIndex[bid] = i;
                mm.SetInstanceTransform(i, Transform3D.Identity);
            }

            _multimeshes[shapeName] = mmi;
        }
    }

    // Cylinder の軸補正: Godot の CylinderMesh は軸 Y、PyBullet GEOM_CYLINDER は軸 Z。
    // 後段で T_pose * T_cylinder_correction を適用することで、メッシュの幾何的な軸 (Y) を
    // ブロックの論理的な軸 (Z) に揃える。X 軸まわり +90° 回転で Y→Z にマップ。
    private static readonly Transform3D _cylinderCorrection = new(
        new Basis(new Quaternion(new Vector3(1, 0, 0), Mathf.Pi / 2)),
        Vector3.Zero);

    private void SetInstanceTransform(int bid, Transform3D t)
    {
        if (!_blockInfo.TryGetValue(bid, out var info)) return;
        string shapeName = info["shape"].AsString();
        if (!_multimeshes.TryGetValue(shapeName, out var mmi)) return;
        if (!_instanceIndex.TryGetValue(bid, out int idx)) return;

        // Cylinder のみ軸補正を適用（他形状は不要）。
        Transform3D effective = t;
        string typeStr = info.ContainsKey("type") ? info["type"].AsString() : "";
        if (typeStr == "cylinder")
        {
            effective = t * _cylinderCorrection;
        }

        mmi.Multimesh.SetInstanceTransform(idx, effective);
    }

    private static Mesh BuildMesh(GDictionary info)
    {
        string stype = info.ContainsKey("type") ? info["type"].AsString() : "box";
        var dimsRaw = info.ContainsKey("dims") ? info["dims"].AsGodotArray() : new GArray();

        switch (stype)
        {
            case "box":
            {
                var bm = new BoxMesh
                {
                    Size = new Vector3(
                        GetFloatOr(dimsRaw, 0, 0.05f),
                        GetFloatOr(dimsRaw, 1, 0.05f),
                        GetFloatOr(dimsRaw, 2, 0.05f)),
                };
                return bm;
            }
            case "cylinder":
            {
                float r = GetFloatOr(dimsRaw, 0, 0.025f);
                float h = GetFloatOr(dimsRaw, 1, 0.06f);
                return new CylinderMesh
                {
                    TopRadius = r,
                    BottomRadius = r,
                    Height = h,
                };
            }
            case "triangular_prism":
            {
                float leg = GetFloatOr(dimsRaw, 0, 0.05f);
                float prismLength = GetFloatOr(dimsRaw, 1, 0.05f);
                return BuildTriangularPrismMesh(leg, prismLength);
            }
            default:
            {
                GD.PushWarning($"BuildMesh: unknown type '{stype}', falling back to default box");
                var fb = new BoxMesh { Size = new Vector3(0.05f, 0.05f, 0.05f) };
                return fb;
            }
        }
    }

    /// <summary>
    /// 直角二等辺三角柱の ArrayMesh を生成。
    /// サーバ側 (sim/blocks.py の _triangular_prism_vertices) と同じ頂点配置を再現する。
    /// 軸は X、断面は YZ 平面、centroid 中心。8 三角形フェイス（前面・後面・底面・側面・斜面）。
    /// 各フェイスに per-face normal を持たせて flat shading 化（lighting が綺麗に出る）。
    /// </summary>
    private static Mesh BuildTriangularPrismMesh(float leg, float prismLength)
    {
        float L = leg;
        float P = prismLength / 2f;
        float cy = L / 3f;
        float cz = L / 3f;

        // 共有頂点（face ごとに重複させて per-face normal を割り当てる前の元位置）
        var v0 = new Vector3(-P, 0f - cy, 0f - cz);  // back, right-angle
        var v1 = new Vector3(-P, L - cy, 0f - cz);   // back, y-leg
        var v2 = new Vector3(-P, 0f - cy, L - cz);   // back, z-leg
        var v3 = new Vector3( P, 0f - cy, 0f - cz);  // front, right-angle
        var v4 = new Vector3( P, L - cy, 0f - cz);   // front, y-leg
        var v5 = new Vector3( P, 0f - cy, L - cz);   // front, z-leg

        // 8 三角形フェイス。各 winding は (b-a)×(c-a) が outward 法線を生む向きで揃える。
        // （元の sim/blocks.py の indices は PyBullet 凸包用で normal 方向を気にしないため、
        //  そのまま Godot ArrayMesh に持ってくると裏返しになるので winding を直す。）
        var faces = new (Vector3 a, Vector3 b, Vector3 c)[]
        {
            (v0, v2, v1),               // 後面 (-X)
            (v3, v4, v5),               // 前面 (+X)
            (v0, v4, v3), (v0, v1, v4), // 底面 (-Z, y-leg rectangle)
            (v0, v5, v2), (v0, v3, v5), // 側面 (-Y, z-leg rectangle)
            (v1, v5, v4), (v1, v2, v5), // 斜面 (+Y, +Z, hypotenuse)
        };

        var vertices = new List<Vector3>(faces.Length * 3);
        var normals = new List<Vector3>(faces.Length * 3);
        foreach (var (a, b, c) in faces)
        {
            // 法線は外向き ((b-a)×(c-a)) のまま採用（lighting 用）。
            var n = (b - a).Cross(c - a).Normalized();
            // ただし Godot は「時計回り = 表面」。faces は外から見て CCW で並べてあるため
            // そのままだと外面がカリングされ透けて見える。頂点を a,c,b の順で出して CW にする。
            vertices.Add(a); vertices.Add(c); vertices.Add(b);
            normals.Add(n);  normals.Add(n);  normals.Add(n);
        }

        var arrays = new GArray();
        arrays.Resize((int)Mesh.ArrayType.Max);
        arrays[(int)Mesh.ArrayType.Vertex] = vertices.ToArray();
        arrays[(int)Mesh.ArrayType.Normal] = normals.ToArray();

        var mesh = new ArrayMesh();
        mesh.AddSurfaceFromArrays(Mesh.PrimitiveType.Triangles, arrays);
        return mesh;
    }

    private static float GetFloatOr(GArray arr, int idx, float fallback)
    {
        if (idx < arr.Count) return arr[idx].AsSingle();
        return fallback;
    }

    private static Color ArrayToColor(GArray arr)
    {
        float r = 1.0f, g = 1.0f, b = 1.0f, a = 1.0f;
        if (arr.Count >= 1) r = arr[0].AsSingle();
        if (arr.Count >= 2) g = arr[1].AsSingle();
        if (arr.Count >= 3) b = arr[2].AsSingle();
        if (arr.Count >= 4) a = arr[3].AsSingle();
        return new Color(r, g, b, a);
    }
}
