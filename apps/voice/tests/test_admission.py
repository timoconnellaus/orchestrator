"""Exercise pinned SDK admission with the production server's configuration."""

import copy
from types import SimpleNamespace

import pytest
from livekit.agents import AgentServer
from livekit.agents.worker import _DefaultLoadCalc

from orchestrator_voice.worker import server


@pytest.mark.parametrize(("jobs", "available"), [(0, True), (1, True), (2, False)])
async def test_voice_admission_counts_jobs_not_unrelated_mac_cpu(
    monkeypatch: pytest.MonkeyPatch, jobs: int, available: bool
) -> None:
    # Reproduce the observed ~93% system CPU while our voice worker is idle.
    monkeypatch.setattr(_DefaultLoadCalc, "_instance", SimpleNamespace(_get_avg=lambda: 0.95))
    monkeypatch.setattr(AgentServer, "active_jobs", property(lambda _: [object()] * jobs))
    probe = copy.copy(server)
    probe._load_fnc = server.load_fnc or _DefaultLoadCalc.get_load
    probe._devmode = False
    probe._draining = False
    probe._reserved_slots = 0
    await probe._refresh_worker_load()
    assert probe._is_available() is available


async def test_pending_assignment_reserves_capacity_before_a_job_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_DefaultLoadCalc, "_instance", SimpleNamespace(_get_avg=lambda: 0.0))
    monkeypatch.setattr(AgentServer, "active_jobs", property(lambda _: []))
    probe = copy.copy(server)
    probe._load_fnc = server.load_fnc or _DefaultLoadCalc.get_load
    probe._devmode = False
    probe._draining = False
    probe._reserved_slots = 1
    await probe._refresh_worker_load()
    assert not probe._is_available()
    monkeypatch.setattr(AgentServer, "active_jobs", property(lambda _: [object()]))
    await probe._refresh_worker_load()
    assert not probe._is_available()  # One active plus one reservation fills both slots.
    probe._reserved_slots = 0
    assert probe._is_available()
    probe._draining = True
    assert not probe._is_available()
