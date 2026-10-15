import json
import time
from unittest.mock import patch

import pytest
from textual.widgets import Static

from fluxen.config import Settings
from fluxen.runs import Runs
from fluxen.tui import Fluxen


def test_workspace_runs_shared_and_isolated(tmp_path):
    first = Runs(tmp_path / "workspace-one")
    second = Runs(tmp_path / "workspace-one")
    other = Runs(tmp_path / "workspace-two")
    first.start("Fix a bug", "demo")
    second.start("Review tests", "reviewer")
    assert len(first.list()) == 2
    assert other.list() == []
    first.update(status="Waiting approval", progress="write_file")
    record = next(row for row in second.list() if row["id"] == first.current["id"])
    assert record["status"] == "Waiting approval"
    first.update(status="Completed")
    assert next(row for row in second.list() if row["id"] == first.current["id"])["status"] == "Completed"


def test_stale_and_corrupt_runs(tmp_path):
    runs = Runs(tmp_path)
    runs.start("Task", "demo")
    record = dict(runs.current, updated=time.time() - 60)
    path = runs.directory / (record["id"] + ".json")
    path.write_text(json.dumps(record))
    (runs.directory / "broken.json").write_text("{")
    assert runs.list()[0]["status"] == "Interrupted"


@pytest.mark.asyncio
async def test_dashboard_mount_and_run_completion(tmp_path):
    with patch("fluxen.session.user_data_dir", return_value=str(tmp_path / "data")):
        app = Fluxen(tmp_path, Settings())

    async def complete(prompt):
        await app.emit("status", "Step 1/12")

    app.agent.run = complete
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.query_one("#agents", Static) is not None
        worker = app.run_agent("Demo task")
        await worker.wait()
        await pilot.pause()
        assert app.runs.list()[0]["status"] == "Completed"
        assert hasattr(app.log, "system")
