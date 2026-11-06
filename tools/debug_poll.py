import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hwdock.app import DockApp
from hwdock.config import Config
from hwdock import win32core as wc

class DebugApp(DockApp):
    n = 0
    def _on_poll(self):
        DebugApp.n += 1
        if DebugApp.n % 20 == 0:
            print('poll', DebugApp.n, 'vis', round(self.vis, 3), 'rect', self.rect,
                  'hidden', self.hidden, 'anim', self.anim, 'drag', self.dragging,
                  'cursor', wc.get_cursor_pos(), 'hover_tab', self.hover_tab, flush=True)
        super()._on_poll()

app = DebugApp(Config.load())
app.run()
