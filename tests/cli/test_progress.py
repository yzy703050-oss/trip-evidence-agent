import asyncio
from types import SimpleNamespace

import pytest
from support.native import ScriptedModel


@pytest.mark.asyncio
async def test_progress_tracks_actual_model_and_tools_and_resets(tmp_path):
    from trip_assistant.bootstrap import create_application
    from trip_assistant.runtime.progress import progress_scope
    from trip_assistant.settings import Settings

    model = ScriptedModel(
        [
            SimpleNamespace(
                content=[
                    {
                        "type": "tool_use",
                        "id": "r",
                        "name": "read",
                        "input": {"request_kind": "lookup", "resource": "trip_summaries"},
                    }
                ]
            ),
            "没有偏好",
        ]
    )
    app = create_application(
        Settings(_env_file=None, single_pass_planning=False),
        model=model,
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="s",
    )
    updates = []
    with progress_scope(updates.append):
        result = await app.run("查看偏好")
    assert result["final_answer"] == "没有偏好"
    assert "正在读取相关资料…" in updates
    assert updates[-1] == "正在整理查询结果与回复…"
    before = len(updates)
    model.script = ["你好"]
    await app.run("你好")
    assert len(updates) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_cli_spinner_closes_on_success_error_and_cancel(failure):
    from trip_assistant.cli import TripEvidenceCLI
    from trip_assistant.runtime.progress import report_progress
    from trip_assistant.settings import Settings

    class Console:
        closed = False
        updates = []

        def status(self, text, **kwargs):
            self.updates.append(text)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def update(self, text):
            self.updates.append(text)

        def print(self, *args, **kwargs):
            pass

    class App:
        async def run(self, query):
            report_progress("正在检查并保存行程…")
            if failure:
                raise failure()
            return {"status": "completed", "final_answer": "已保存"}

    console = Console()
    cli = TripEvidenceCLI(
        settings=Settings(_env_file=None, single_pass_planning=False), console=console
    )
    cli.app = App()
    if failure:
        with pytest.raises(failure):
            await cli.process_query("规划")
    else:
        await cli.process_query("规划")
    assert console.closed and "正在检查并保存行程…" in console.updates


@pytest.mark.asyncio
async def test_broken_progress_display_does_not_fail_model_request(tmp_path):
    from trip_assistant.bootstrap import create_application
    from trip_assistant.runtime.progress import progress_scope
    from trip_assistant.settings import Settings

    model = ScriptedModel(["你好"])
    app = create_application(
        Settings(_env_file=None, single_pass_planning=False),
        model=model,
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="s",
    )

    def broken(text):
        raise OSError("display closed")

    with progress_scope(broken):
        result = await app.run("你好")
    assert result["status"] == "completed" and result["final_answer"] == "你好"
