import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hwdock.app import DockApp, _premultiply
from hwdock.config import Config

app = DockApp(Config.load())
app.sampler.start(); app.sensors.start(); app.hardware.start()
app._create_window()
app.vis = 1.0
app._invalidate()
img = app._get_panel()
print('panel size', img.size, 'mode', img.mode, flush=True)
print('alpha extrema', img.getchannel('A').getextrema(), flush=True)
print('center px', img.getpixel((img.width//2, img.height//2)), flush=True)
pre = _premultiply(img, app.cfg.opacity)
print('opacity', app.cfg.opacity, 'pre alpha extrema', pre.getchannel('A').getextrema(), flush=True)
print('pre center', pre.getpixel((img.width//2, img.height//2)), flush=True)
app._present()
print('rect', app.rect, flush=True)
app._message_loop()
