import sys, os, time, faulthandler
faulthandler.enable()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hwdock.app import DockApp
from hwdock.config import Config
from hwdock.render import PAGE_HW

cfg = Config.load(); cfg.page = PAGE_HW; cfg.edge = 'right'
app = DockApp(cfg)
app.sampler.start(); app.sensors.start(); app.hardware.start()
app._create_window()
app.vis = 1.0
print('page', app.page, flush=True)
app._invalidate()
img = app._get_panel()
print('hw panel', img.size, flush=True)
img.save('tools/build/hw_direct.png')
app._present()
print('presented', app.rect, flush=True)
time.sleep(1)
app._invalidate()
app._present()
print('second present ok', flush=True)
app._message_loop()
