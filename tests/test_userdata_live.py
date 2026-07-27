"""Static consistency checks for deploy/userdata/live.sh.

実 AWS を叩けないので、代わりに「壊れると学習成果が消える」不変条件を
スクリプト本文に対して静的に固定する。

背景: Spot 中断ハンドラが `/opt/bs/state/` しか S3 へ sync しておらず、
学習成果である `/opt/bs/models/`（snapshot）が退避されていなかった。
Spot 中断 terminate でその日の学習が EBS ごと消える状態だったため、
同じ抜けが再発しないようテストで固定する。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_SH = REPO_ROOT / "deploy" / "userdata" / "live.sh"
LIVE_SERVER = REPO_ROOT / "src" / "block_stacker" / "serving" / "live_server.py"

# S3 prefix <-> ローカルディレクトリの対応を取り出す（{} にローカル側の名前を入れる）
_RESTORE_RE = r"aws s3 sync s3://\"?\$?\{{?APP_BUCKET\}}?\"?/(\S+)\s+/opt/bs/{}/"
_FLUSH_RE = r"aws s3 sync /opt/bs/{}/\s+s3://\$\{{APP_BUCKET\}}/(\S+)"


@pytest.fixture(scope="module")
def script() -> str:
    return LIVE_SH.read_text(encoding="utf-8")


class TestFlushToS3:
    """停止・中断時に snapshot が S3 へ退避されること。"""

    def test_flush_script_syncs_models(self, script: str) -> None:
        # ここが抜けていたのが元のバグ。models/ は snapshot の置き場所。
        assert re.search(r"aws s3 sync\s+/opt/bs/models/\s+s3://", script), \
            "flush が /opt/bs/models/ を S3 へ退避していない（学習成果が失われる）"

    def test_flush_script_syncs_world_state(self, script: str) -> None:
        assert re.search(r"aws s3 sync\s+/opt/bs/state/\s+s3://", script)

    def test_container_is_stopped_before_sync(self, script: str) -> None:
        """docker stop が sync より前にあること。

        live_server は SIGTERM を受けて初めて snapshot を書き出すので、
        順序が逆だと古い snapshot しか S3 に上がらない。
        """
        stop_at = script.index("docker stop -t")
        sync_at = script.index("aws s3 sync /opt/bs/models/")
        assert stop_at < sync_at, "docker stop より先に models を sync している"

    def test_spot_handler_calls_the_shared_flush(self, script: str) -> None:
        """Spot ハンドラが独自 sync ではなく共通スクリプトを呼ぶこと。"""
        handler = script[script.index("spot_handler.sh <<"):]
        assert "/usr/local/bin/bs_flush_s3.sh" in handler

    def test_shutdown_unit_calls_the_shared_flush(self, script: str) -> None:
        """通常停止（stop-instances → systemd shutdown）でも退避すること。"""
        assert re.search(r"ExecStop=/usr/local/bin/bs_flush_s3\.sh", script)
        assert "systemctl enable --now bs-flush.service" in script


class TestRestorePath:
    """次回起動時に S3 から戻せること（退避先と復元元の prefix 一致）。"""

    def test_boot_restores_models_from_the_same_prefix(self, script: str) -> None:
        restore = re.search(_RESTORE_RE.format("models"), script)
        flush = re.search(_FLUSH_RE.format("models"), script)
        assert restore and flush, "models の復元/退避のどちらかが見つからない"
        assert restore.group(1).rstrip("/") == flush.group(1).rstrip("/"), \
            f"復元元 {restore.group(1)!r} と退避先 {flush.group(1)!r} の prefix が不一致"

    def test_boot_restores_world_state_from_the_same_prefix(self, script: str) -> None:
        restore = re.search(_RESTORE_RE.format("state"), script)
        flush = re.search(_FLUSH_RE.format("state"), script)
        assert restore and flush
        assert restore.group(1).rstrip("/") == flush.group(1).rstrip("/")


class TestDailyStopStartLoop:
    def test_restart_policy_survives_a_manual_stop(self, script: str) -> None:
        """bs-flush.service が docker stop するので `always` でないと翌日復帰しない。

        `unless-stopped` は手動停止したコンテナを daemon 再起動時に起こさない。
        """
        assert "--restart always" in script
        assert "--restart unless-stopped" not in script


class TestGraceBudget:
    """Spot 猶予 2 分に収まり、live_server 側の待ち時間と揃っていること。"""

    def test_docker_stop_grace_matches_live_server_timeout(self, script: str) -> None:
        m = re.search(r"docker stop -t (\d+)", script)
        assert m, "docker stop に猶予秒数 (-t) が指定されていない"
        grace = int(m.group(1))

        from block_stacker.serving.live_server import SNAPSHOT_SAVE_TIMEOUT_SEC
        assert grace == int(SNAPSHOT_SAVE_TIMEOUT_SEC), (
            f"docker stop -t {grace} と SNAPSHOT_SAVE_TIMEOUT_SEC "
            f"{SNAPSHOT_SAVE_TIMEOUT_SEC} が不一致。短い方で snapshot が切られる"
        )

    def test_grace_fits_in_the_spot_two_minute_window(self, script: str) -> None:
        grace = int(re.search(r"docker stop -t (\d+)", script).group(1))
        poll = int(re.search(r"sleep (\d+)\s*\ndone", script).group(1))
        # 検知遅延 + docker stop + 1.6GB upload (~15s) < 120s
        assert poll + grace + 15 < 120, "Spot の猶予 2 分に収まらない"


class TestLiveServerShutdown:
    """SIGTERM で snapshot 保存パスが走ること（これが無いと sync しても中身が古い）。"""

    def test_sigterm_handler_is_installed(self) -> None:
        import asyncio

        from block_stacker.serving.live_server import install_shutdown_handlers

        async def run() -> tuple[list[str], bool]:
            loop = asyncio.get_running_loop()
            event = asyncio.Event()
            names = install_shutdown_handlers(loop, event)
            return names, event.is_set()

        names, already_set = asyncio.run(run())
        assert "SIGTERM" in names, \
            "SIGTERM ハンドラが入っていない（docker stop で snapshot が消える）"
        assert not already_set

    def test_source_wires_the_signal_into_the_shutdown_path(self) -> None:
        src = LIVE_SERVER.read_text(encoding="utf-8")
        assert "install_shutdown_handlers(asyncio.get_running_loop()" in src
        # duration 到達だけでなくシグナルでも待機が解けること
        assert "return_when=asyncio.FIRST_COMPLETED" in src
