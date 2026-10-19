"""Real native CLI fixture; no legacy orchestration facade."""

import io

from rich.console import Console

from support.native import ScriptedModel
from trip_assistant.cli import TripEvidenceCLI
from trip_assistant.settings import Settings


def app_for(root):
    app = TripEvidenceCLI(
        settings=Settings(_env_file=None, single_pass_planning=False),
        storage_root=root,
        model=ScriptedModel(["回复"]),
        providers={},
        console=Console(file=io.StringIO(), width=250, color_system=None),
    )
    app.user_id = "alice"
    app._bind_session("current")
    return app
