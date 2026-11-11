import copy
import math
from datetime import datetime,timezone
import pytest
from tdoc_unit_converter import UnitConverter, convert_boundary_envelope, compare_boundary_envelopes
from tdoc_unit_converter.errors import ConversionError, DataSemanticError, TimeReferenceError, FrameMismatchError
from tdoc_unit_converter.adapters import wgs84_to_ecef, ecef_to_wgs84, altitude_to_ned_down, ned_down_to_altitude

HASH='a'*64

def envelope(semantic='navigation_velocity', *, unit='m/s', frame='NED', value=(4.,5.,6.), covariance=None,
             data_role='current_state', source_time_s=20., **kwargs):
    if covariance is None: covariance=[[4.,0.,0.],[0.,9.,0.],[0.,0.,16.]] if isinstance(value,(tuple,list)) else [[.04]]
    out={'schema':'TDDT-ISO-CrossLayerEnvelope-v1','semantic':semantic,'data_role':data_role,
         'producer':'MESA_NAV','transaction_id':'t-1','snapshot_hash':HASH,'epoch_id':'sim-1',
         'clock_domain':'PLANT_ELAPSED_S','source_time_s':source_time_s,
         'unit':unit,'frame':frame,'value':value,'covariance':covariance}
    out.update(kwargs)
    return out

def run(x=None,**kwargs):
    x=x or envelope()
    return convert_boundary_envelope(x,target_unit=kwargs.pop('target_unit','m/s'),target_frame=kwargs.pop('target_frame','ENU'),
                                     expected_semantic=kwargs.pop('expected_semantic','navigation_velocity'),
                                     expected_role=kwargs.pop('expected_role','current_state'),
                                     reference_time_s=kwargs.pop('reference_time_s',20.1),expected_epoch_id=kwargs.pop('expected_epoch_id','sim-1'),**kwargs)

def test_ned_enu_roundtrip_covariance_and_sign():
    orig=envelope()
    a=run(orig)
    assert a['value']==(5,4,-6)
    assert a['covariance']==((9.,0.,0.),(0.,4.,0.),(0.,0.,16.))
    assert a['unit']=='m/s' and a['frame']=='ENU'
    assert a['native_controller_certified'] is False and a['flight_authorized'] is False
    b=run(a,target_frame='NED')
    assert b['value']==tuple(orig['value'])
    assert b['covariance']==tuple(tuple(row) for row in orig['covariance'])
    assert b['frame_history']==['NED->ENU','ENU->NED']
    assert len(a['transform_sha256'])==64


def test_units_and_covariance_scaled_once():
    x=envelope(unit='ft/s',value=[10.,20.,-30.],covariance=[[1.,0,0],[0.,4.,0],[0,0,9.]])
    y=run(x,target_unit='m/s')
    f=.3048
    assert y['value']==pytest.approx((20*f,10*f,30*f))
    assert [y['covariance'][i][i] for i in range(3)]==pytest.approx([4*f*f,1*f*f,9*f*f])
    assert y['normalization_history']==['ft/s->m/s']
    z=run(y,target_unit='m/s',target_frame='ENU')
    assert z['value']==pytest.approx(y['value'])
    assert z['covariance']==y['covariance']

@pytest.mark.parametrize('semantic', ['pilot_throttle_reference','post_mixer_motor_command','jsbsim_motor_position_norm'])
def test_dimensionless_command_semantics_disjoint(semantic):
    x=envelope(semantic,unit='1',frame='SCALAR',value=.3,covariance=[[.0025]],data_role='reference')
    got=run(x,target_unit='1',target_frame='SCALAR',expected_semantic=semantic,expected_role='reference')
    assert got['semantic']==semantic and got['value']==.3
    for target in {'pilot_throttle_reference','post_mixer_motor_command','jsbsim_motor_position_norm'}-{semantic}:
        with pytest.raises(DataSemanticError,match='semantic_or_role_mismatch'):
            run(x,target_unit='1',target_frame='SCALAR',expected_semantic=target,expected_role='reference')

def test_truth_does_not_become_navigation_state():
    x=envelope(data_role='truth')
    with pytest.raises(DataSemanticError,match='truth_cannot_be_navigation'):
        run(x,expected_role='truth')
    with pytest.raises(DataSemanticError):run(x)

@pytest.mark.parametrize('field,value,error',[
    ('schema','WRONG',DataSemanticError),('semantic','motor_speed_measured',DataSemanticError),
    ('producer','',DataSemanticError),('transaction_id','',DataSemanticError),
    ('snapshot_hash','short',DataSemanticError),('epoch_id','different',TimeReferenceError),
    ('clock_domain','UTC',TimeReferenceError),('source_time_s',1.,TimeReferenceError),
    ('source_time_s',21.,TimeReferenceError),('source_time_s',float('nan'),ConversionError),
    ('frame','ENU_PSEUDO',FrameMismatchError),('value',(1,2),ConversionError),
    ('value',(1,2,float('inf')),ConversionError),
])
def test_bad_authority_or_time_or_value_fails_closed(field,value,error):
    x=envelope(**{field:value})
    with pytest.raises(error):run(x)

def test_producer_snapshot_transaction_mismatch_rejected():
    for field, expected in [('expected_producer','ARDUPILOT'),('expected_transaction_id','t-2'),('expected_snapshot_hash','b'*64)]:
        with pytest.raises(DataSemanticError):run(expected_epoch_id='sim-1',**{field:expected})

def test_negative_nonfinite_asymmetric_and_non_psd_covariance():
    mats=([
        [-1.,0.,0.],[0.,1.,0.],[0.,0.,1.]],
        [[1.,1.,0.],[0.,1.,0.],[0.,0.,1.]],
        [[1.,2.,0.],[2.,1.,0.],[0.,0.,1.]],
        [[1.,0.,0.],[0.,math.nan,0.],[0.,0.,1.]],
        [[1.,0.],[0.,1.]],
    )
    for M in mats:
        with pytest.raises(ConversionError):run(envelope(covariance=M))

def test_repeated_normalizations_rejected():
    x=envelope(unit='ft/s',normalization_history=['ft/s->m/s'])
    with pytest.raises(DataSemanticError,match='repeated_unit'):run(x)
    x=envelope(frame_history=['NED->ENU'])
    with pytest.raises(DataSemanticError,match='repeated_frame'):run(x)

def test_frd_flu_accel_covariance():
    x=envelope('body_acceleration',unit='m/s2',frame='FRD',value=[1.,-3.,4.],covariance=[[1,0,0],[0,2,0],[0,0,3]])
    y=run(x,expected_semantic='body_acceleration',target_frame='FLU',target_unit='m/s2')
    assert y['value']==(1,3,-4)
    assert [y['covariance'][i][i] for i in range(3)]==[1,2,3]

def test_different_frames_must_use_explicit_adapter():
    x=envelope()
    with pytest.raises(FrameMismatchError):run(x,target_frame='FRD')


def test_conversion_keeps_source_dict_immutable():
    orig=envelope(); before=copy.deepcopy(orig)
    run(orig)
    assert orig==before

def test_compare_prior_vs_simulation_observation_diagnostic_only():
    p=envelope('jsbsim_motor_position_norm',unit='1',frame='SCALAR',value=.3,covariance=[[.04]],data_role='prior')
    o=envelope('jsbsim_motor_position_norm',unit='1',frame='SCALAR',value=.4,covariance=[[.01]],data_role='simulation_observation')
    def get(x):
        return run(x,expected_semantic='jsbsim_motor_position_norm',expected_role=x['data_role'],target_unit='1',target_frame='SCALAR')
    result=compare_boundary_envelopes(get(p),get(o))
    assert result['residual']==pytest.approx(.1)
    assert result['dynamic_validation_certified'] is False
    assert result['flight_authorized'] is False

@pytest.mark.parametrize('field,value',[
    ('transaction_id','t-2'),('snapshot_hash','b'*64),('epoch_id','epoch-B'),('semantic','post_mixer_motor_command'),
    ('frame','ENU'),('unit','%')
])
def test_compare_mismatched_prediction_observation_rejected(field,value):
    p=envelope('jsbsim_motor_position_norm',unit='1',frame='SCALAR',value=.3,data_role='prior')
    o=envelope('jsbsim_motor_position_norm',unit='1',frame='SCALAR',value=.4,data_role='simulation_observation')
    o[field]=value
    with pytest.raises(DataSemanticError):compare_boundary_envelopes(p,o)

def test_compare_mismatched_plant_time_and_roles():
    a=envelope('jsbsim_motor_position_norm',unit='1',frame='SCALAR',value=.3,data_role='prior')
    b=envelope('jsbsim_motor_position_norm',unit='1',frame='SCALAR',value=.4,data_role='simulation_observation',source_time_s=20.2)
    with pytest.raises(TimeReferenceError):compare_boundary_envelopes(a,b)
    got=compare_boundary_envelopes(a,b,time_tolerance_s=.21)
    assert got['pair_delta_time_s']==pytest.approx(.2)
    b['data_role']='truth'
    with pytest.raises(DataSemanticError):compare_boundary_envelopes(a,b,time_tolerance_s=.21)


def test_unit_converter_rejects_nan_inf_negative_uncertainty():
    c=UnitConverter()
    for v in (math.nan,math.inf,-math.inf,True):
        with pytest.raises(ConversionError):c.convert(v,'ft','m')
    with pytest.raises(ConversionError):c.convert(1,'ft','m',uncertainty=-.2)
    assert c.convert(1,'ft','m',uncertainty=.2).uncertainty==pytest.approx(.06096)


def test_wgs_ecef_and_altitude_reference_regression():
    lat,lon,h=math.radians(40.015),math.radians(-105.27),1650
    xyz=wgs84_to_ecef(lat,lon,h)
    lat2,lon2,h2=ecef_to_wgs84(*xyz)
    assert lat2==pytest.approx(lat,abs=1e-11)
    assert lon2==pytest.approx(lon,abs=1e-11)
    assert h2==pytest.approx(h,abs=1e-5)
    assert altitude_to_ned_down(1150,1200)==50
    assert ned_down_to_altitude(50,1200)==1150
