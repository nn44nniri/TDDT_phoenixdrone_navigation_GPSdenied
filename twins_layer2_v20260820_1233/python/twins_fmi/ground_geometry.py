"""SI geometry contract shared by Layer2 and TDOC; no contact-force claim."""
import math

REVISION = 'phoenix-fourfoot-r3.02-v1'
NAMES = ('TAIL_FOOT_L1','TAIL_FOOT_L2','TAIL_FOOT_R1','TAIL_FOOT_R2')


def structural_to_frd(point,cg):
    if len(point)!=3 or len(cg)!=3 or not all(math.isfinite(float(v)) for v in (*point,*cg)):
        raise ValueError('finite structural point and CG required')
    return [float(cg[0])-float(point[0]),float(point[1])-float(cg[1]),float(cg[2])-float(point[2])]


def body_to_ned(point,q):
    if len(point)!=3 or len(q)!=4 or not all(math.isfinite(float(v)) for v in (*point,*q)):
        raise ValueError('finite BODY_FRD point and quaternion required')
    norm=math.sqrt(sum(float(v)**2 for v in q))
    if not .95<=norm<=1.05:raise ValueError('valid FRD-to-NED quaternion required')
    w,x,y,z=(float(v)/norm for v in q);a,b,c=map(float,point)
    return [(1-2*(y*y+z*z))*a+2*(x*y-w*z)*b+2*(x*z+w*y)*c,
            2*(x*y+w*z)*a+(1-2*(x*x+z*z))*b+2*(y*z-w*x)*c,
            2*(x*z-w*y)*a+2*(y*z+w*x)*b+(1-2*(x*x+y*y))*c]


class GroundGeometry:
    def __init__(self,profile):
        data=profile.get('ground_geometry') or {}
        if (data.get('schema')!='TDDT-Phoenix-ground-geometry-v1' or
            data.get('revision')!=REVISION or data.get('body_frame')!='BODY_FRD_CG' or
            data.get('structural_frame')!='JSBSIM_AFT_RIGHT_UP' or data.get('unit')!='m'):
            raise ValueError('versioned four-foot BODY_FRD_CG geometry required')
        self.data=data;self.contacts=data.get('contacts') or []
        if tuple(c.get('name') for c in self.contacts)!=NAMES:
            raise ValueError('exact four named feet required')
        for c in self.contacts:
            expected=structural_to_frd(c['structural_m'],data['cg_structural_m'])
            if c.get('type')!='STRUCTURE' or len(c['body_frd_m'])!=3 or any(not math.isfinite(float(a)) or abs(float(a)-b)>1e-12 for a,b in zip(c['body_frd_m'],expected)):
                raise ValueError('structural/FRD/CG contact mismatch')
        if len({tuple(c['body_frd_m']) for c in self.contacts})!=4:
            raise ValueError('distinct foot locations required')
        pitch=math.radians(float(data['reset_pitch_deg']))
        height=self.height_agl((math.cos(pitch/2),0.,math.sin(pitch/2),0.))
        declared=float(data['cg_height_agl_at_reset_m'])
        if not math.isfinite(declared) or abs(declared-height)>1e-9:
            raise ValueError('declared CG AGL contradicts geometry')

    def height_agl(self,q):
        height=max(body_to_ned(c['body_frd_m'],q)[2] for c in self.contacts)
        if height<=0:raise ValueError('pose does not place feet below CG')
        return height

    def clearances(self,cg_agl_m,q):
        if not math.isfinite(float(cg_agl_m)):raise ValueError('finite CG AGL required')
        return {c['name']:float(cg_agl_m)-body_to_ned(c['body_frd_m'],q)[2] for c in self.contacts}
