"""A finished job must not leave a server running on the host."""

from app.workspace_agent import WorkspaceAgent


class Store:
    status = "running"

    def get(self, job_id):
        return {"id": job_id, "status": self.status, "steps": 0, "max_steps": 1}

    def set_status(self, job_id, status, **kwargs):
        self.status = status

    def step(self, *args, **kwargs):
        return None

    def save_trajectory(self, *args, **kwargs):
        return None

    def audit(self, *args, **kwargs):
        return None


def test_stop_background_clears_the_record():
    agent = WorkspaceAgent(Store(), llm=None, grant_checker=lambda job_id: True)
    agent._background["j1"] = [999999]  # a pid that does not exist
    agent._stop_background("j1")
    assert agent._background.get("j1") is None


def test_stop_background_tolerates_missing_pids():
    agent = WorkspaceAgent(Store(), llm=None, grant_checker=lambda job_id: True)
    agent._stop_background("never-seen")  # must not raise
