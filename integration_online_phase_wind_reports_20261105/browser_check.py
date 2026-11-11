import base64, hashlib, json, os, signal, socket, struct, subprocess, time
from pathlib import Path
from urllib.request import urlopen
A=Path('integration_online_phase_wind_reports_20261105/browser_preview');A.mkdir(exist_ok=True)
class CDP:
 def __init__(self,url):
  from urllib.parse import urlparse
  u=urlparse(url);self.s=socket.create_connection((u.hostname,u.port),timeout=20);self.i=0
  key=base64.b64encode(os.urandom(16)).decode()
  req=f'GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
  self.s.sendall(req.encode());hdr=b''
  while not hdr.endswith(b'\r\n\r\n'):hdr+=self.s.recv(1)
  assert hdr.startswith(b'HTTP/1.1 101 '),hdr
 def read(self,n):
  b=b''
  while len(b)<n:
   x=self.s.recv(n-len(b))
   if not x:raise RuntimeError('websocket closed')
   b+=x
  return b
 def call(self,method,params=None):
  self.i+=1;p=json.dumps({'id':self.i,'method':method,'params':params or {}}).encode();n=len(p);mask=os.urandom(4)
  header=bytes([129,128|n]) if n<126 else bytes([129,254])+struct.pack('!H',n) if n<65536 else bytes([129,255])+struct.pack('!Q',n)
  self.s.sendall(header+mask+bytes(c^mask[i%4] for i,c in enumerate(p)))
  total=b''
  while True:
   a,b=self.read(2);length=b&127
   if length==126:length=struct.unpack('!H',self.read(2))[0]
   if length==127:length=struct.unpack('!Q',self.read(8))[0]
   masking=self.read(4) if b&128 else None
   data=self.read(length)
   if masking:data=bytes(c^masking[i%4] for i,c in enumerate(data))
   total+=data
   if a&128:
    msg=json.loads(total);total=b''
    if msg.get('id')==self.i:
     if 'error'in msg:raise RuntimeError(msg['error'])
     return msg.get('result',{})
 def js(self,expr):
  r=self.call('Runtime.evaluate',{'expression':expr,'returnByValue':True})
  if r.get('exceptionDetails'):raise RuntimeError(r['exceptionDetails'])
  return r['result'].get('value')
port=19843
args=['/usr/bin/google-chrome','--headless','--no-sandbox','--no-first-run','--disable-background-networking','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--user-data-dir=/tmp/tddt-phase-cdp-20261105','--allow-file-access-from-files','--window-size=1750,1800',f'--remote-debugging-port={port}','about:blank']
log=(A/'cdp_chrome.log').open('w');env=os.environ.copy();env['TMPDIR']='/tmp'
proc=subprocess.Popen(args,stdout=log,stderr=log,env=env,start_new_session=True)
try:
 for i in range(40):
  try:
   tabs=json.load(urlopen(f'http://localhost:{port}/json/list'));break
  except OSError:time.sleep(.2)
 c=CDP(next(t['webSocketDebuggerUrl'] for t in tabs if t['type']=='page'))
 c.call('Page.enable');c.call('Page.navigate',{'url':Path('TDOC_v20260911_2052/reports/online/mission/MISSION_ROUTE_3D.html').resolve().as_uri()})
 for i in range(60):
  ready=c.js("(()=>{const p=document.querySelectorAll('.js-plotly-plot');return p.length===4&&!!p[1]._fullData&&p[1]._transitionData? p[1]._transitionData._frames.length:0})()")
  if ready==5:break
  time.sleep(.3)
 assert ready==5,ready
 c.js("window.detail=document.querySelectorAll('.js-plotly-plot')[1];document.getElementById('route-section-1').scrollIntoView();true")
 diag={'frames':ready,'stages':[]}
 for frame,count in [(0,102),(2,141),(3,79),(4,53),(1,4083)]:
  c.js(f"Plotly.animate(window.detail,['{frame}'],{{mode:'immediate',frame:{{duration:0,redraw:true}},transition:{{duration:0}}}});true")
  for _ in range(40):
   r=c.js("({title:window.detail.layout.title.text,points:window.detail._fullData[0].x.length,heads:window.detail._fullData[3].x.length})")
   if r['points']==count:break
   time.sleep(.25)
  assert r['points']==count,r
  diag['stages'].append({'frame':frame,**r})
  if frame==2:
   time.sleep(1)
   capture=c.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
   (A/'phase_landing_verified.png').write_bytes(base64.b64decode(capture['data']))
 c.js("document.getElementById('route-section-0').scrollIntoView();true");time.sleep(1)
 capture=c.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
 (A/'map_wind_verified.png').write_bytes(base64.b64decode(capture['data']))
 diag['main_wind_heads']=c.js("document.querySelectorAll('.js-plotly-plot')[0]._fullData.filter(t=>t.type==='cone').map(t=>t.x.length)")
 (A/'render_diagnostics.json').write_text(json.dumps(diag,ensure_ascii=False,indent=2))
 print(json.dumps(diag,ensure_ascii=False,indent=2))
finally:
 os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=10);log.close()
