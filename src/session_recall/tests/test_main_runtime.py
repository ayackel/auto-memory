import io
import sys
import types

import pytest

from session_recall import __main__ as cli_main


class _DummyConn:
    def close(self):
        return None


def test_main_flushes_output_before_telemetry_and_capture(monkeypatch):
    events = []

    class TrackingStdout(io.StringIO):
        def flush(self):
            events.append("flush")
            return super().flush()

    def fake_run(_args):
        print("visible-output", end="")
        events.append("command")
        return 0

    def fake_record(**_kwargs):
        events.append("telemetry")

    def fake_capture(_args):
        events.append("capture")

    monkeypatch.setattr(cli_main.efficacy, "init", lambda _path: _DummyConn())
    monkeypatch.setattr(cli_main.telemetry, "init", lambda _path: None)
    monkeypatch.setattr(cli_main.telemetry, "record", fake_record)
    monkeypatch.setattr(cli_main.capture, "run", fake_capture)
    monkeypatch.setitem(
        sys.modules,
        "session_recall.commands.list_sessions",
        types.SimpleNamespace(run=fake_run),
    )

    out = TrackingStdout()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "argv", ["auto-memory", "list"])

    with pytest.raises(SystemExit) as ex:
        cli_main.main()
    assert ex.value.code == 0
    assert out.getvalue() == "visible-output"
    assert events.index("flush") < events.index("telemetry")
    assert events.index("flush") < events.index("capture")
