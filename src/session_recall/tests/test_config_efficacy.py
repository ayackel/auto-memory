import importlib


def test_efficacy_config_defaults(monkeypatch):
    for var in ("SESSION_RECALL_EFFICACY_DB", "SESSION_RECALL_RETENTION_DAYS",
                "SESSION_RECALL_CAPTURE_BUDGET_MS", "SESSION_RECALL_NO_CAPTURE",
                "COPILOT_AGENT_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    import session_recall.config as cfg
    importlib.reload(cfg)
    assert cfg.EFFICACY_DB_PATH.endswith("session-recall-efficacy.db")
    assert cfg.RETENTION_DAYS == 90
    assert cfg.CAPTURE_BUDGET_MS == 150
    assert cfg.NO_CAPTURE is False
    assert cfg.AGENT_SESSION_ID is None


def test_efficacy_config_env_override(monkeypatch):
    monkeypatch.setenv("SESSION_RECALL_NO_CAPTURE", "1")
    monkeypatch.setenv("SESSION_RECALL_RETENTION_DAYS", "30")
    monkeypatch.setenv("COPILOT_AGENT_SESSION_ID", "abc")
    import session_recall.config as cfg
    importlib.reload(cfg)
    assert cfg.NO_CAPTURE is True
    assert cfg.RETENTION_DAYS == 30
    assert cfg.AGENT_SESSION_ID == "abc"
