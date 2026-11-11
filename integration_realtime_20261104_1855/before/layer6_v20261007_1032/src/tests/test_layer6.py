import time, pytest
from layer6.contracts import DecisionEnvelope, PilotCommand
from layer6.validation import validate
from layer6.executor import DigitalPilotExecutor

class FakeClient:
 def __init__(self): self.events=[]
 def state(self,*a,**kw): self.events.append(('state',a,kw))
 def action(self,*a,**kw): self.events.append(('action',a,kw))
 def release(self): self.events.append(('release',(),{}))

def test_l5_order_and_ranges():
 e=DecisionEnvelope.from_dict({'u_cmd':[.1,-.2,.55,0],'C_exec':{'direct_pwm':False},'S_safe':{'psf':True,'rta':True}})
 assert e.command==PilotCommand(.1,-.2,.55,0,None,())
 assert validate(e).ok

def test_reject_direct_pwm():
 e=DecisionEnvelope.from_dict({'u_cmd':[0,0,.5,0],'C_exec':{'direct_pwm':True}})
 assert not validate(e).ok

def test_deadline_failclosed():
 e=DecisionEnvelope('x',PilotCommand(0,0,.5,0),tau_deadline=time.time()-1)
 assert 'after_tau_deadline' in validate(e).reasons

def test_executor_routes_gui_state_and_action():
 c=FakeClient(); x=DigitalPilotExecutor(c,30)
 e=DecisionEnvelope('x',PilotCommand(.1,-.2,.5,.3,'QHOVER',('ARM',)),tau_deadline=time.time()+2,constraints={'direct_pwm':False},safety={'psf':True,'rta':True})
 x.accept(e); assert x.tick();
 assert any(z[0]=='action' and z[1][0]=='qhover' for z in c.events)
 assert any(z[0]=='action' and z[1][0]=='arm' for z in c.events)
 assert any(z[0]=='state' for z in c.events)
