import io
import json
from rich.console import Console
from cli import TripEvidenceCLI
from agents.workflow_guard import check_workflow, finalize_workflow
from workflow_support import populated_workflow
from utils.response_renderer import finalize_business_result


def test_cli_displays_the_same_validated_route_without_old_guard(monkeypatch):
    import agents.itinerary_module as module
    def fail(*args): raise AssertionError('legacy guard must not run')
    monkeypatch.setattr(module, 'guard_final_itinerary', fail)
    w = populated_workflow(); checked = check_workflow(w); w = finalize_workflow(w, checked)
    app = TripEvidenceCLI(); output = io.StringIO(); app.console = Console(file=output, width=240, color_system=None)
    before = json.dumps(w, sort_keys=True)
    app._display_results(finalize_business_result({'status': 'completed', 'final_answer': '已整理', 'workflow': w,
                          'workflow_id': w['id'], 'validated_plan': checked, 'stop_reason': 'validated'}))
    assert '北京' in output.getvalue() and '杭州' in output.getvalue()
    assert '2026-10-15' in output.getvalue()
    assert '尚未核实' in output.getvalue()
    assert json.dumps(w, sort_keys=True) == before
