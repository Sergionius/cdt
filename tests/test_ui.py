from cdt import config, ui


class FakeTable:
    def __init__(self, **kwargs):
        self.rows = []

    def add_column(self, *args, **kwargs):
        pass

    def add_row(self, *values):
        self.rows.append(values)


class FakeLive:
    def __init__(self, renderable, **kwargs):
        self.renderable = renderable
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def update(self, renderable):
        self.renderable = renderable


def test_pretty_tracker_tracks_status_and_duration(monkeypatch):
    clock = iter([10.0, 10.0, 12.5])
    monkeypatch.setattr(ui, "Console", lambda: object())
    monkeypatch.setattr(ui, "Table", FakeTable)
    monkeypatch.setattr(ui, "Live", FakeLive)
    monkeypatch.setattr(ui.time, "monotonic", lambda: next(clock))

    tracker = ui._PrettyTracker()
    tracker.set("Build", "running")
    tracker.set("Build", "done")

    assert tracker.stages == [("Build", "done")]
    assert tracker.stage_duration["Build"] == 2.5
    assert tracker._format_duration(5) == "5s"
    assert tracker._format_duration(65) == "1m 5s"
    assert tracker.live.renderable.rows == [("Build", "✅ done (2s)")]


def test_tracker_start_and_stop_with_tty(monkeypatch):
    fake = FakeLive(FakeTable())

    class Tracker:
        def __init__(self):
            self.live = fake

        def start(self):
            fake.start()

        def stop(self):
            fake.stop()

    monkeypatch.setattr(config, "UI_MODE", "pretty")
    monkeypatch.setattr(ui, "Console", object)
    monkeypatch.setattr(ui, "Live", object)
    monkeypatch.setattr(ui, "Table", object)
    monkeypatch.setattr(ui, "_PrettyTracker", Tracker)
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm")

    ui._tracker_start()
    ui._tracker_stop()

    assert fake.started is True
    assert fake.stopped is True
    assert ui._PRETTY_TRACKER is None


def test_tracker_falls_back_to_text(monkeypatch, capsys):
    monkeypatch.setattr(config, "UI_MODE", "pretty")
    monkeypatch.setattr(ui, "_PRETTY_TRACKER", None)

    ui._tracker_set("Upload", "running")
    ui._ui_echo("visible")

    assert capsys.readouterr().out == "Upload: running\nvisible\n"


def test_ui_echo_is_suppressed_while_live_tracker_exists(monkeypatch, capsys):
    monkeypatch.setattr(ui, "_PRETTY_TRACKER", object())

    ui._ui_echo("hidden")

    assert capsys.readouterr().out == ""
