import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_update.py"
CRON_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "intelligence_feed_update.py"


def load_script():
    spec = importlib.util.spec_from_file_location("intelligence_feed_run_update", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cron_script():
    spec = importlib.util.spec_from_file_location("intelligence_feed_cron_update", CRON_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_prints_machine_readable_result(capsys):
    module = load_script()
    result = SimpleNamespace(to_dict=lambda: {
        "run_id": 7,
        "status": "partial",
        "inserted_count": 3,
        "merged_count": 1,
        "error_count": 1,
        "started_source_count": 4,
    })

    assert module.main(lambda: result) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == 7
    assert payload["status"] == "partial"


def test_main_returns_one_without_leaking_exception(capsys):
    module = load_script()

    def fail():
        raise RuntimeError("https://host.invalid/?api_key=DO-NOT-LEAK")

    assert module.main(fail) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "intelligence-feed update failed"
    assert "DO-NOT-LEAK" not in captured.err


def test_cron_script_exit_status_tracks_collection_result(capsys):
    module = load_cron_script()
    success = SimpleNamespace(status="partial", to_dict=lambda: {"status": "partial"})
    failed = SimpleNamespace(status="failed", to_dict=lambda: {"status": "failed"})
    assert module.main(lambda: success) == 0
    assert module.main(lambda: failed) == 1
    assert [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()] == ["partial", "failed"]
