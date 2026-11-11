#!/usr/bin/env python3
import argparse,json,socket,time
p=argparse.ArgumentParser(); p.add_argument('--endpoint',default='127.0.0.1:14661'); p.add_argument('--mode',default='QHOVER'); a=p.parse_args()
h,port=a.endpoint.rsplit(':',1)
pkt={'transaction_id':'example-'+str(int(time.time())), 'u_cmd':[0.10,-0.05,0.55,0.0],
     'C_exec':{'pilot_domain':{'a':[-1,1],'e':[-1,1],'t':[0,1],'r':[-1,1]},'direct_pwm':False},
     'S_safe':{'psf':True,'rta':True}, 'tau_apply':time.time()-0.1,'tau_deadline':time.time()+5,
     'rho_decision':0.95,'nu':'example-v1',
     'execution_intent':{'requested_pilot_mode':a.mode,'discrete':[]}}
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2); s.sendto(json.dumps(pkt).encode(),(h,int(port)))
try: print(s.recvfrom(4096)[0].decode())
except TimeoutError: print('sent; no ACK received')
