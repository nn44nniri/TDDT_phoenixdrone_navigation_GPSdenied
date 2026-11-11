#!/usr/bin/env python3
from __future__ import annotations
import argparse, random, time
from .contracts import DecisionEnvelope, PilotCommand
from .executor import DigitalPilotExecutor

# Randomized values are bounded, while flight-phase order is fixed.
PHASES=[
 ('ground','QHOVER',(0,0,0,0),('THROTTLE_ZERO',),1.0),
 ('arm','QHOVER',(0,0,0,0),('ARM',),2.0),
 ('vertical_takeoff','QHOVER',(.03,-.02,.62,.02),(),3.0),
 ('hover','QHOVER',(.02,.01,.50,.02),(),2.0),
 ('vtol_translation','QHOVER',(.05,-.18,.54,.02),(),3.0),
 ('transition','FBWA',(.04,-.08,.61,.01),(),3.0),
 ('cruise','FBWA',(.10,.01,.58,.04),(),5.0),
 ('approach','FBWA',(.04,.05,.43,.01),(),3.0),
 ('back_transition','QHOVER',(.03,.06,.48,.01),(),3.0),
 ('vtol_approach','QHOVER',(.03,.10,.43,.02),(),3.0),
 ('final_hover','QHOVER',(0,0,.50,0),(),2.0),
 ('descent','QHOVER',(0,0,.38,0),(),3.0),
 ('touchdown','QHOVER',(0,0,0,0),('THROTTLE_ZERO',),1.0),
 ('disarm','QHOVER',(0,0,0,0),('DISARM',),2.0),]

def jitter(v,amount,rng,lo=-1,hi=1): return max(lo,min(hi,v+rng.uniform(-amount,amount)))
def main():
 p=argparse.ArgumentParser(); p.add_argument('--seed',type=int,default=42); p.add_argument('--time-scale',type=float,default=1.0); p.add_argument('--rate-hz',type=float,default=30); a=p.parse_args()
 rng=random.Random(a.seed); ex=DigitalPilotExecutor(rate_hz=a.rate_hz)
 for i,(name,mode,u,disc,dur) in enumerate(PHASES):
  aa=jitter(u[0],.03,rng); ee=jitter(u[1],.03,rng); tt=jitter(u[2],.025,rng,0,1); rr=jitter(u[3],.025,rng)
  env=DecisionEnvelope(f'demo-{i:02d}-{name}',PilotCommand(aa,ee,tt,rr,mode,disc),tau_deadline=time.time()+max(2,dur*a.time_scale+2),constraints={'direct_pwm':False},safety={'psf':True,'rta':True},version='demo-v1')
  print(f'[DEMO] {name:18s} mode={mode:10s} u={[round(x,3) for x in (aa,ee,tt,rr)]} discrete={disc}',flush=True)
  ex.accept(env); ex.hold_for(dur*a.time_scale)
 ex.client.release(); print('[DEMO] complete')
if __name__=='__main__': main()
