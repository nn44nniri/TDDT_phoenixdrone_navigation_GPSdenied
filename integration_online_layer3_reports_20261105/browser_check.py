import base64, hashlib, json, os, signal, socket, struct, subprocess, time
from pathlib import Path
from urllib.request import urlopen
A=Path('integration_online_layer3_reports_20261105/browser_preview');A.mkdir(exist_ok=True)
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
import csv
port=19844
args=['/usr/bin/google-chrome','--headless','--no-sandbox','--no-first-run','--disable-background-networking','--user-data-dir=/tmp/tddt-layer3-cdp-20261105','--allow-file-access-from-files','--window-size=1750,1750',f'--remote-debugging-port={port}','about:blank']
log=(A/'cdp_chrome.log').open('w');env=os.environ.copy();env['TMPDIR']='/tmp'
proc=subprocess.Popen(args,stdout=log,stderr=log,env=env,start_new_session=True)
try:
 for i in range(40):
  try:
   tabs=json.load(urlopen(f'http://localhost:{port}/json/list'));break
  except OSError:time.sleep(.2)
 c=CDP(next(t['webSocketDebuggerUrl'] for t in tabs if t['type']=='page'))
 c.call('Page.enable');c.call('Page.navigate',{'url':Path('TDOC_v20260911_2052/reports/online/mission/LAYER3_DYNAMIC_NETWORK_REPORT.html').resolve().as_uri()})
 for i in range(80):
  ready=c.js('window.TDDT_L3_RENDER_STATE||null')
  if ready and ready['cycle']==1:break
  time.sleep(.2)
 assert ready and ready['cycle']==1,ready
 with Path('TDOC_v20260911_2052/reports/online/mission/LAYER3_NETWORK_TIMELINE.csv').open() as f: expected=list(csv.DictReader(f))
 diag={'initial':ready,'cycles':[]}
 c.js("document.getElementById('l3-feedback-next').click();true")
 for i in range(40):
  state=c.js('window.TDDT_L3_RENDER_STATE')
  if state['cycle']==4:break
  time.sleep(.1)
 assert state['cycle']==4 and state['model_version']==1,state
 diag['first_feedback']=state
 for value in [63,64,999,4457]:
  c.js(f"document.getElementById('l3-cycle').value={value};document.getElementById('l3-cycle').dispatchEvent(new Event('change'));true")
  for i in range(60):
   state=c.js('window.TDDT_L3_RENDER_STATE')
   if state['cycle']==value+1:break
   time.sleep(.1)
  assert state['cycle']==value+1 and state['integrity']=='PASS' and state['edge_count']==5,state
  assert state['model_version']==int(expected[value]['feedback_version'])
  c.js("document.getElementById('l3-role').value='snapshot';document.getElementById('l3-role').dispatchEvent(new Event('change'));true")
  snap=c.js("JSON.parse(document.getElementById('l3-packet').textContent).snapshot_hash")
  assert snap==expected[value]['snapshot_hash']
  assert c.js("Number(document.getElementById('cycle').value)")==value
  diag['cycles'].append(state)
 c.js("document.querySelector('#l3-graph g[aria-label=WingMPET]').dispatchEvent(new MouseEvent('click',{bubbles:true}));true")
 assert c.js("JSON.parse(document.getElementById('l3-packet').textContent).twin_id")=='WingMPET'
 diag['node_selection_verified']=True
 c.js("document.getElementById('l3-role').value='Layer7';document.getElementById('l3-role').dispatchEvent(new Event('change'));true")
 prior=c.js("JSON.parse(document.getElementById('l3-packet').textContent).prior_consumed_by_snapshot")
 assert prior['model_update']['feedback_version']==1665
 assert prior['route_hash']==expected[-1]['feedback_route_hash']
 diag['prior_and_current_feedback_separated']=True
 diag['nodes']=c.js("document.querySelectorAll('#l3-graph g[role=button]').length")
 assert diag['nodes']==5
 c.js("document.getElementById('l3-inspector').scrollIntoView();true");time.sleep(.5)
 shot=c.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
 (A/'layer3_network_verified.png').write_bytes(base64.b64decode(shot['data']))
 (A/'render_diagnostics.json').write_text(json.dumps(diag,ensure_ascii=False,indent=2))
 print(json.dumps(diag,ensure_ascii=False,indent=2))
finally:
 os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=10);log.close()
