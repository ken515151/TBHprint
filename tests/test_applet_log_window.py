"""The tray's Log window on a real Tk (runs where a display exists - the Windows CI job; skipped
headless). Owner 2026-11-03: "each new log line creation still makes the scrolled window onto the
bottom even though you may be reviewing lines above"."""

import pytest

tk = pytest.importorskip("tkinter")

from tbhprint.applet import windows  # noqa: E402


class FakeApp:
    def __init__(self, root):
        self.root = root
        self.lines = [f"09:{i:02d} line {i}" for i in range(300)]

    def try_call(self, cmd, **args):
        assert cmd == "get_log_tail"
        return list(self.lines)


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display for Tk: {exc}")
    root.withdraw()
    yield root
    root.destroy()


def test_log_window_keeps_the_readers_place_and_follows_the_tail_only_from_the_bottom(root):
    app = FakeApp(root)
    window = windows.LogWindow(app)
    window.top.update()
    assert window.text.yview()[1] >= 0.999, "opens at the newest line"

    window.text.yview("50.0")
    window.top.update()
    top_before = window.text.get("@0,0 linestart", "@0,0 lineend")
    app.lines = app.lines[5:] + [f"10:{i:02d} new {i}" for i in range(5)]   # five roll off, five arrive
    window.refresh()
    window.top.update()
    assert window.text.get("@0,0 linestart", "@0,0 lineend") == top_before, "reading above: the view stays"

    window.text.see("end")
    window.top.update()
    app.lines = app.lines[1:] + ["11:00 newest"]
    window.refresh()
    window.top.update()
    assert window.text.yview()[1] >= 0.999, "at the bottom: follows the tail"
    assert window.text.get("end-1c linestart", "end-1c") == "11:00 newest"
    window.close()
