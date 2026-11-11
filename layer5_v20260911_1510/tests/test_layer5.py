from __future__ import annotations
import copy, json, os, tempfile, unittest
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT/'src'))
from layer5.config import Layer5Config
from layer5.compute import EdgeProfileManager
from layer5.contract import Layer4ContractAdapter, IntegrityFreshnessGate, AdmissibilitySupervisor
from layer5.decision import ObjectiveNormalizer, LexicographicDecisionEngine, ConfidenceEngine, OperationalModeManager
from layer5.engine import Layer5Engine
from layer5.environment import Layer2VirtualEnvironmentAdapter, PTLibJSBSimAdapter
from layer5.execution import DecisionExplainer, Layer6Assembler, Layer7EvidenceRouter
from layer5.experience import EpisodeAggregator
from layer5.knowledge import KStore, KnowledgeReferenceResolver
from layer5.phoenix import PhoenixSpecialization
from layer5.policy import PolicyAdapter
from layer5.registry import CandidateRegistry
from layer5.qlpv import QLPVCorrectionKernel
from layer5.safety import PredictiveSafetyFilter, BaselineSafeController, RuntimeAssurance
from layer5.tdoc import TDOCManager

BASE=yaml.safe_load((ROOT/'configs/configuration_phoenix.yaml').read_text())
FIX=json.loads((ROOT/'examples/layer4_result_verified_TEST_FIXTURE.json').read_text())
REAL=json.loads((ROOT/'examples/layer4_result_unverified_example.json').read_text())
TDOC=Path(os.environ['TDDT_UNIT_ADAPTER_ROOT']).resolve() if os.environ.get('TDDT_UNIT_ADAPTER_ROOT') else None
PTLIB=Path(os.environ['TDDT_PTLIB_ROOT']).resolve() if os.environ.get('TDDT_PTLIB_ROOT') else None
_BASE_KSTORE_ENV=os.environ.get('TDDT_KSTORE_ROOT')

def cfg(tmp=None, fixtures=False, auto=.6, backend='FILE'):
    d=copy.deepcopy(BASE); d.setdefault('testing',{})['allow_fixture_verification']=fixtures; d['confidence']['auto_threshold']=auto; d['confidence']['auto_risk_max']=0.2
    if tmp:
        d['knowledge']['directory']=str(Path(tmp)/'kstore')
        current=os.environ.get('TDDT_KSTORE_ROOT')
        if current in (None,_BASE_KSTORE_ENV) or (current and not Path(current).exists()):
            os.environ['TDDT_KSTORE_ROOT']=str(Path(tmp)/'kstore')
    elif _BASE_KSTORE_ENV is not None:
        os.environ['TDDT_KSTORE_ROOT']=_BASE_KSTORE_ENV
    d['knowledge']['backend']=backend
    return Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml')

class TestLayer5(unittest.TestCase):
    def test_01_layer4_boundary_no_actuator(self):
        c=cfg(); t=TDOCManager(c); self.assertTrue(t.validate_layer4_envelope(FIX)['ok'])
    def test_02_layer4_boundary_rejects_actuator(self):
        d=copy.deepcopy(FIX); d['actuator_command']=True; self.assertFalse(TDOCManager(cfg()).validate_layer4_envelope(d)['ok'])
    def test_03_layer4_boundary_rejects_network_commit(self):
        d=copy.deepcopy(FIX); d['network_commit']=True; self.assertFalse(TDOCManager(cfg()).validate_layer4_envelope(d)['ok'])
    def test_04_fixture_verification_rejected_by_default(self):
        cs=Layer4ContractAdapter(cfg()).candidates(FIX); self.assertFalse(cs[0].verified)
    def test_05_fixture_verification_allowed_only_testing(self):
        cs=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX); self.assertTrue(cs[0].verified)
    def test_06_hand_edited_verified_rejected(self):
        d=copy.deepcopy(FIX); d['provenance'].pop('test_fixture',None); d['candidates'][0]['provenance'].pop('test_fixture',None); d['candidates'][0]['verification']['executed']=False
        self.assertFalse(Layer4ContractAdapter(cfg()).candidates(d)[0].verified)
    def test_06b_persisted_positive_ff_certificate_is_admissible_authority(self):
        d=copy.deepcopy(FIX); d['provenance'].pop('test_fixture',None); c=d['candidates'][0]; c['provenance'].pop('test_fixture',None)
        c['verification'].update({'executed':False,'attempted':False,'verified':True,
            'outcome_status':'ATTEMPTED_AND_VERIFIED','verification_origin':'FF_CERT_REUSE',
            'attempt_kind':'CERTIFICATE_REUSE','current_cycle_ff_executed':False,
            'certificate_persisted':True,'ff_origin':True,'certificate_id':'durable-positive-cert'})
        self.assertTrue(Layer4ContractAdapter(cfg()).candidates(d)[0].verified)
    def test_06c_unpersisted_or_negative_certificate_is_rejected(self):
        d=copy.deepcopy(FIX); d['provenance'].pop('test_fixture',None); c=d['candidates'][0]; c['provenance'].pop('test_fixture',None)
        c['verification'].update({'executed':False,'attempted':False,'verified':True,
            'outcome_status':'ATTEMPTED_AND_VERIFIED','verification_origin':'FF_CERT_REUSE',
            'attempt_kind':'CERTIFICATE_REUSE','current_cycle_ff_executed':False,
            'certificate_persisted':False,'ff_origin':True,'certificate_id':'not-durable'})
        self.assertFalse(Layer4ContractAdapter(cfg()).candidates(d)[0].verified)
        c['verification'].update({'certificate_persisted':True,'verification_origin':'FF_REJECTION_CERT_REUSE'})
        self.assertFalse(Layer4ContractAdapter(cfg()).candidates(d)[0].verified)
    def test_07_ci_version(self):
        c=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX)[0]; c.snapshot_version='bad'; self.assertFalse(IntegrityFreshnessGate(cfg()).evaluate(c,FIX).ok)
    def test_08_ci_hash(self):
        c=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX)[0]; c.snapshot_hash='bad'; self.assertFalse(IntegrityFreshnessGate(cfg()).evaluate(c,FIX).ok)
    def test_09_ci_expired_epoch(self):
        d=copy.deepcopy(FIX); d['provenance']['snapshot_epoch_s']=100.0; c=Layer4ContractAdapter(cfg(fixtures=True)).candidates(d)[0]
        self.assertIn('expired_snapshot',IntegrityFreshnessGate(cfg()).evaluate(c,d,now_s=200.0).reasons)
    def test_10_admissibility_verified(self):
        c=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX)[0]; a=AdmissibilitySupervisor(cfg(fixtures=True)).evaluate(c); self.assertTrue(a.ok)
    def test_11_admissibility_unverified(self):
        c=Layer4ContractAdapter(cfg()).candidates(FIX)[0]; self.assertIn('not_full_fidelity_verified',AdmissibilitySupervisor(cfg()).evaluate(c).reasons)
    def test_12_admissibility_risk(self):
        c=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX)[0]; c.violation_probability=.9; self.assertIn('chance_risk',AdmissibilitySupervisor(cfg()).evaluate(c).reasons)
    def test_13_phoenix_four_actuators(self):
        p=PhoenixSpecialization(cfg()); self.assertEqual(p.PHYSICAL_ACTUATORS,['left_elevon','right_elevon','left_motor','right_motor'])
    def test_14_phoenix_transition_model_and_controller_are_distinct(self):
        ph=PhoenixSpecialization(cfg())
        self.assertTrue(ph.transition_validated)
        self.assertFalse(ph.vtol_autopilot_controller_validated)
    def test_15_phoenix_differential_thrust_disabled(self): self.assertEqual(PhoenixSpecialization(cfg()).rudd_dt_gain,0.0)
    def test_16_phoenix_direct_pwm_disabled(self): self.assertFalse(PhoenixSpecialization(cfg()).direct_pwm_allowed)
    def test_17_phoenix_motor_parameters(self):
        p=PhoenixSpecialization(cfg()); self.assertAlmostEqual(p.omega_max,800.0); self.assertAlmostEqual(p.k_t,7.864e-6); self.assertAlmostEqual(p.tau_up,.016)
    def test_18_unvalidated_l4_to_phoenix_mapping_fails(self):
        p=PhoenixSpecialization(cfg()); d=copy.deepcopy(FIX['candidates'][0]); d.pop('logical_actions'); seq,rec=p.bind_candidate(d); self.assertFalse(rec['ok']); self.assertEqual(seq,[])
    def test_19_explicit_phoenix_logical_sequence_passes(self):
        p=PhoenixSpecialization(cfg()); seq,rec=p.bind_candidate(FIX['candidates'][0]); self.assertTrue(rec['ok']); self.assertEqual(len(seq[0]),4)
    def test_20_qlpv_zero_correction_without_calibrated_model(self):
        c=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX)[0]; p=PhoenixSpecialization(cfg()); c.logical_actions=p.bind_candidate(c.raw)[0]
        q=QLPVCorrectionKernel(cfg(),p).solve(c); self.assertTrue(q['ok']); self.assertEqual(q['solver_mode'],'verification_preserving_zero_correction'); self.assertEqual(q['corrected_actions'],c.logical_actions)
    def test_21_psf_rejects_bounds(self):
        p=PhoenixSpecialization(cfg()); r=PredictiveSafetyFilter(cfg(),p).filter([2,0,.5,0],True); self.assertFalse(r['gate'])
    def test_22_rta_fallback(self):
        fb=BaselineSafeController(cfg()).action(); r=RuntimeAssurance().choose({'gate':False},fb,False); self.assertTrue(r['fallback_used'])
    def test_23_lexicographic_selects_existing_candidate(self):
        cs=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX); ranked,rec=LexicographicDecisionEngine(cfg()).rank(cs); self.assertEqual(ranked[0].candidate_id,'TEST-C1'); self.assertEqual(set(rec.ranking),{'TEST-C1','TEST-C2'})
    def test_24_confidence_not_twin_weight(self):
        cs=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX); gate=IntegrityFreshnessGate(cfg()); adm=AdmissibilitySupervisor(cfg(fixtures=True)); p=PhoenixSpecialization(cfg())
        for c in cs: c.ci=gate.evaluate(c,FIX); c.adm=adm.evaluate(c); c.logical_actions=p.bind_candidate(c.raw)[0]; c.solver=QLPVCorrectionKernel(cfg(),p).solve(c)
        rho,parts=ConfidenceEngine(cfg()).compute(cs,cs[0]); self.assertTrue(0<=rho<=1); self.assertIn('q_FF',parts)
    def test_25_mode_advisory_never_commits(self):
        m=OperationalModeManager(cfg()).resolve(.2,.01,True,False,'ADVISORY'); self.assertFalse(m['commit_authorized'])
    def test_25b_online_autonomous_only_uses_bounded_sitl_risk_without_human(self):
        c=cfg(); c.data.setdefault('operational',{})['authority_profile']='AUTONOMOUS_ONLY'; c.data['operational']['online_simulation_auto_risk_max']=0.10
        m=OperationalModeManager(c).resolve(.875,.068318,True,False,'AUTO',learning_mode='ONLINE_JSBSIM')
        self.assertEqual(m['mode'],'AUTO'); self.assertTrue(m['commit_authorized']); self.assertFalse(m['human_required']); self.assertTrue(m['auto_eligible'])
    def test_25c_online_autonomous_only_fails_closed_to_hold_above_sitl_risk(self):
        c=cfg(); c.data.setdefault('operational',{})['authority_profile']='AUTONOMOUS_ONLY'; c.data['operational']['online_simulation_auto_risk_max']=0.10
        m=OperationalModeManager(c).resolve(.875,.12,True,False,'AUTO',learning_mode='ONLINE_JSBSIM')
        self.assertEqual(m['mode'],'HOLD'); self.assertFalse(m['commit_authorized']); self.assertFalse(m['human_required']); self.assertEqual(m['mode_reason'],'no_auto_eligible_candidate')
    def test_26_real_layer4_demo_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td)).run(REAL).to_dict(); self.assertTrue(r['fallback']['used']); self.assertEqual(r['operational_mode'],'HOLD'); self.assertFalse(r['gates']['FINAL'])
    def test_27_verified_fixture_default_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=False)).run(FIX).to_dict(); self.assertTrue(r['fallback']['used'])
    def test_28_verified_fixture_offline_normal_path(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=.60)).run(FIX).to_dict(); self.assertFalse(r['fallback']['used']); self.assertTrue(r['gates']['FINAL']); self.assertIsNotNone(r['control_action']); self.assertIsNone(r['layer6_package']); self.assertFalse(r['network_commit']); self.assertFalse(r['model_retraining']); self.assertFalse(r['direct_pwm'])
    def test_29_only_first_move_committed(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=.60)).run(FIX).to_dict(); self.assertEqual(r['control_action']['first_move'],FIX['candidates'][0]['logical_actions'][0])
    def test_30_offline_route_never_layer6(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=.60)).run(FIX,'OFFLINE_SE').to_dict(); self.assertFalse(r['provenance']['environment_route']['layer6_call'])
    def test_31_online_requires_valid_setd(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=.60)).run(FIX,'ONLINE_JSBSIM').to_dict(); self.assertTrue(r['fallback']['used']); self.assertIn('valid_SETD',str(r['gates']['lifecycle']))
    def test_32_kstore_file_resume_finalize(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); k=KStore(c); eid=k.start_episode('E1'); k.append({'x':1}); k2=KStore(c); self.assertEqual(k2.episode_id,'E1'); self.assertEqual(k2.sequence,1); k2.append({'x':2}); mf=k2.finalize({'k':'v'}); self.assertTrue(mf['committed']); self.assertTrue(KStore(c).load_setd()['ok'])
    def test_33_kstore_single_episode(self):
        with tempfile.TemporaryDirectory() as td:
            k=KStore(cfg(td)); k.start_episode('E1'); k.append({'x':1}); k.finalize({});
            with self.assertRaises(RuntimeError): KStore(cfg(td)).start_episode('E2')

    def test_33b_completed_kstore_fails_closed_before_offline_decision(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td,fixtures=True,auto=.60)
            k=KStore(c); k.start_episode('E1'); k.append({'x':1}); k.finalize({})
            r=Layer5Engine(c).run(FIX,'OFFLINE_SE').to_dict()
            self.assertTrue(r['fallback']['used'])
            self.assertFalse(r['gates']['FINAL'])
            self.assertFalse(r['gates']['lifecycle']['ok'])
            self.assertEqual(r['gates']['lifecycle']['episode_admission'],'rejected')
            self.assertIn('offline episode limit=1 already consumed',str(r['gates']['lifecycle']['reasons']))
            self.assertFalse(r['experience']['recorded'])

    def test_33c_kstore_status_exposes_offline_episode_admission(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); k=KStore(c)
            self.assertTrue(k.status()['offline_episode_startable'])
            k.start_episode('E1')
            self.assertFalse(k.status()['offline_episode_startable'])
            k.append({'x':1}); k.finalize({})
            status=KStore(c).status()
            self.assertTrue(status['offline_episode_completed'])
            self.assertFalse(status['offline_episode_startable'])
    def test_34_kstore_embedded_db(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td,backend='EMBEDDED_DB'); k=KStore(c); k.start_episode('E1'); k.append({'x':1}); self.assertTrue(k.finalize({})['committed']); self.assertTrue(KStore(c).load_setd()['ok'])
    def test_35_knowledge_reference_requires_hash(self):
        r=KnowledgeReferenceResolver(cfg()).resolve({'CRP':{'id':'1','version':'v1'}}); self.assertFalse(r['ok']); self.assertTrue(r['runtime_write']['CRP'] is False)
    def test_36_knowledge_reference_read_only(self):
        r=KnowledgeReferenceResolver(cfg()).resolve({'CRP':{'id':'1','version':'v1','hash':'abc'}}); self.assertTrue(r['ok']); self.assertTrue(r['references']['CRP']['read_only'])
    def test_37_online_with_setd_blocks_model_only_layer6_package(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td,fixtures=True,auto=.60); k=KStore(c); k.start_episode('E1'); k.append({'x':1}); k.finalize({'ready':True})
            r=Layer5Engine(c).run(FIX,'ONLINE_JSBSIM').to_dict()
            self.assertTrue(r['fallback']['used'])
            self.assertIsNone(r['layer6_package'])
            self.assertFalse(r['gates']['FINAL'])
            self.assertTrue(r['knowledge']['setd']['ok'])
            self.assertEqual(r['gates']['native_pilot_controller']['reason'],'native_pilot_controller_prediction_unavailable')
            self.assertEqual(r['provenance']['environment_route']['backend'],'PTLIB_Adapter∘JSBSim_L6')
    def test_38_online_does_not_persist_experience(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td,fixtures=True,auto=.60); k=KStore(c); k.start_episode('E1'); k.append({'x':1}); k.finalize({'ready':True}); root=Path(td)/'kstore'; before=(root/'ss'/'SS_MANIFEST.json').read_bytes()+b''.join(x.read_bytes() for x in sorted((root/'ss'/'segments').glob('segment_*.jsonl')))
            Layer5Engine(c).run(FIX,'ONLINE_JSBSIM'); after=(root/'ss'/'SS_MANIFEST.json').read_bytes()+b''.join(x.read_bytes() for x in sorted((root/'ss'/'segments').glob('segment_*.jsonl'))); self.assertEqual(before,after)
    def test_39_setd_tamper_recovers_latest_verified_commit(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); k=KStore(c); k.start_episode('E1'); k.append({'x':1}); k.finalize({}); p=Path(td)/'kstore'/'SETD_KSTORE.json'; p.write_text('{}')
            r=KStore(c).load_setd(); self.assertTrue(r['ok']); self.assertTrue(r['recovery']['performed']); self.assertEqual(r['recovery']['action'],'load_latest_committed_verified')
    def test_40_tdoc_real_probe_when_available(self):
        old=os.environ.get('TDDT_UNIT_ADAPTER_ROOT')
        try:
            if TDOC is None or not TDOC.exists(): self.skipTest('external TDOC root not configured')
            os.environ['TDDT_UNIT_ADAPTER_ROOT']=str(TDOC)
            t=TDOCManager(cfg()).probe(); self.assertTrue(t['ok']); self.assertEqual(t['version'],'2.2.0')
        finally:
            if old is None: os.environ.pop('TDDT_UNIT_ADAPTER_ROOT',None)
            else: os.environ['TDDT_UNIT_ADAPTER_ROOT']=old
    def test_41_phoenix_motor_prediction_bounded(self):
        p=PhoenixSpecialization(cfg()); r=p.predict_motor_step(0,800,1.0); self.assertLessEqual(r['omega_rad_s'],800); self.assertLessEqual(r['thrust_N'],5.03296+1e-8)
    def test_42_full_envelope_not_enabled_by_cpu_profile(self):
        d=copy.deepcopy(BASE); d['edge']['profile']='EDGE-LIGHT'; c=Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml'); self.assertFalse(PhoenixSpecialization(c).full_envelope)
    def test_43_preflight_core(self):
        with tempfile.TemporaryDirectory() as td: self.assertTrue(Layer5Engine(cfg(td)).preflight(False)['core_resources_ok'])
    def test_44_recommendation_decision_action_separate(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=.60)).run(FIX).to_dict(); self.assertIsInstance(r['recommendation'],dict); self.assertIsInstance(r['decision'],dict); self.assertIsInstance(r['control_action'],dict); self.assertNotEqual(r['recommendation'],r['decision'])

    def test_45_layer2_virtual_adapter_requires_three_twins(self):
        a=Layer2VirtualEnvironmentAdapter(); a.register('WingMPET',lambda u,c:{'t':'W'}); a.register('MESA_NAV',lambda u,c:{'t':'M'}); self.assertFalse(a.ready()); a.register('Climate',lambda u,c:{'t':'C'}); self.assertTrue(a.ready()); self.assertEqual(set(a.rollout({'command':[0,0,.5,0]})),{'WingMPET','MESA_NAV','Climate'})
    def test_46_ptlib_adapter_probe_exact_api_files(self):
        root=PTLIB; old=os.environ.get('TDDT_PTLIB_ROOT')
        try:
            if root is None or not root.exists(): self.skipTest('external PT_LIB root not configured')
            os.environ['TDDT_PTLIB_ROOT']=str(root)
            r=PTLibJSBSimAdapter(cfg()).probe(); self.assertTrue(r['ok']); self.assertIn('PTLibFmiClient.send_pilot_command',str(r['api'])); self.assertEqual(r['fmu']['transport'],'FMI2-CoSimulation'); self.assertFalse(r['fmu']['container_runtime_required'])
        finally:
            if old is None: os.environ.pop('TDDT_PTLIB_ROOT',None)
            else: os.environ['TDDT_PTLIB_ROOT']=old
    def test_47_calibrated_qlpv_path_is_executable(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            d=copy.deepcopy(BASE); d['testing']['allow_fixture_verification']=True; d['phoenix']['mixer']['validated']=True; d['phoenix']['servo_dynamics']['identified']=True; d['phoenix']['servo_dynamics']['tau_s']=[.05,.05]; d['phoenix']['servo_dynamics']['rate_limit_rad_s']=[5.0,5.0]; d['phoenix']['esc_mapping']['identified']=True; d['knowledge']['directory']=str(Path(td)/'kstore')
            cpre=Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml'); ppre=PhoenixSpecialization(cpre); model=Path(td)/'m.npz'
            np.savez(model,A=np.eye(2),B=np.array([[1,0,0,0],[0,1,0,0]],float),calibrated=np.array([1]),phoenix_profile_sha256=np.array([ppre.hash]),airframe_binding_sha256=np.array([ppre.airframe_binding_hash()]))
            d['qlpv']['model_file']=str(model); c0=Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml'); p0=PhoenixSpecialization(c0); c=Layer4ContractAdapter(c0).candidates(FIX)[0]; c.logical_actions=p0.bind_candidate(c.raw)[0]; c.provenance['qlpv_initial_error']=[0.2,-0.1]
            c.provenance['qlpv_chance_certificate']={'verified':True,'epsilon':[0.01,0.01],'version':'TEST-CERT'}
            q=QLPVCorrectionKernel(c0,p0).solve(c); self.assertTrue(q['ok']); self.assertEqual(q['solver_mode'],'calibrated_scheduled_projected_qLPV_correction'); self.assertTrue(q['model']['nonzero_correction_authorized']); self.assertTrue(q['physical_tube']['motor_dynamics_used']); self.assertTrue(q['physical_tube']['servo_dynamics_used'])

    def test_48_compute_deadline_forces_rta_fallback_without_relaxing_safety(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td,fixtures=True,auto=.0); c.data['edge']['deadline_ms']=0.0
            r=Layer5Engine(c).run(FIX).to_dict(); self.assertFalse(r['gates']['budget']['ok']); self.assertFalse(r['gates']['budget']['safety_bounds_relaxed']); self.assertFalse(r['gates']['FINAL']); self.assertTrue(r['fallback']['used'])
    def test_49_cvar_profile_fails_closed_without_tail_evidence(self):
        c0=cfg(fixtures=True); c0.data['admissibility']['cvar_enabled']=True; c=Layer4ContractAdapter(c0).candidates(FIX)[0]; a=AdmissibilitySupervisor(c0).evaluate(c); self.assertIn('missing_cvar_evidence',a.reasons)
    def test_50_guidance_objective_weights_enter_recommendation(self):
        with tempfile.TemporaryDirectory() as td:
            d=copy.deepcopy(FIX); d['provenance']['knowledge']={'objective_weights':{'risk':2.5,'energy':0.5}}
            r=Layer5Engine(cfg(td,fixtures=True,auto=.0)).run(d).to_dict(); w=r['recommendation']['tradeoff']['decision_weights']; self.assertAlmostEqual(sum(w.values()),1.0); self.assertAlmostEqual(w['risk']/w['energy'],5.0)
    def test_51_bounded_sensitivity_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=.0)).run(FIX).to_dict(); self.assertIn('sensitivity',r['gates']); self.assertLessEqual(len(r['gates']['sensitivity']),int(cfg().get('edge.sensitivity_top_k',3)))

    def test_52_environment_uses_unit_adapter_root_only_for_external_tdoc(self):
        text=(ROOT/'configs/environment.sh').read_text()
        self.assertIn('TDDT_UNIT_ADAPTER_ROOT',text)
        self.assertNotIn('export TDDT_TDOC_ROOT=',text)
        self.assertIn('TDOC_ISO_Universal_Unit_Conversion_Standard20260824_0914',text)

    def test_53_environment_declared_deployment_roots(self):
        text=(ROOT/'configs/environment.sh').read_text()
        required=['twins_layer2_v20260820_1233','climate_twin_v20260824_0945','boulder_multilayer_map_builder_v20260814_1215','SARG_v20260821_0945','layer3_v20260907_0608','layer4_v20260907_0608','TDDT_ORCHESTRATOR_ROOT','TDDT_KSTORE_ROOT']
        self.assertTrue(all(x in text for x in required))

    def test_54_candidate_registry_bounded_no_pareto_generation(self):
        cs=Layer4ContractAdapter(cfg(fixtures=True)).candidates(FIX); r=CandidateRegistry(cfg()); r.load(cs); st=r.status(); self.assertTrue(st['ok']); self.assertFalse(st['pareto_generation']); self.assertLessEqual(st['count'],st['max'])

    def test_55_policy_layer7_update_requires_authorization(self):
        p=PolicyAdapter(cfg()); r=p.adapt(FIX,{'version':'v2','delta':{'risk_bias':0.01}}); self.assertFalse(r['layer7_update']['accepted']); self.assertFalse(r['network_commit'])

    def test_56_policy_layer7_update_is_bounded_versioned(self):
        p=PolicyAdapter(cfg()); r=p.adapt(FIX,{'authorized_by_layer7':True,'version':'v2','delta':{'risk_bias':0.05}}); self.assertTrue(r['layer7_update']['accepted']); self.assertEqual(r['theta']['version'],'v2'); self.assertFalse(r['model_retraining'])

    def test_57_policy_rejects_unbounded_delta(self):
        p=PolicyAdapter(cfg()); r=p.adapt(FIX,{'authorized_by_layer7':True,'version':'v2','delta':{'risk_bias':1.0}}); self.assertFalse(r['layer7_update']['accepted']); self.assertEqual(r['layer7_update']['reason'],'delta_norm_exceeds_bound')

    def test_58_explainability_reason_package_present(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=0.0)).run(FIX).to_dict(); e=r['explanation'];
            for k in ['id_selected','rank','tradeoff','risk','uncertainty','J_C','mu','constraints','human','decision_confidence']: self.assertIn(k,e)

    def test_59_online_control_requires_native_controller_not_just_layer6_revalidation(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td,fixtures=True,auto=0.0); k=KStore(c); k.start_episode('E1'); k.append({'x':1}); k.finalize({'ready':True})
            r=Layer5Engine(c).run(FIX,'ONLINE_JSBSIM').to_dict()
            self.assertIsNone(r['layer6_package'])
            self.assertFalse(r['gates']['native_control_ready'])
            self.assertFalse(r['gates']['FINAL'])
            self.assertEqual(r['fallback']['reason'],'native_pilot_controller_prediction_unavailable')

    def test_60_fast_meso_slow_aggregation_and_mrg(self):
        e=EpisodeAggregator(cfg()); refs={'references':{'CRP':{'id':'C1','guidance':{'objective_weights':{'risk':2}}},'SARG':{'id':'S1','guidance':{'objective_weights':{'energy':.5}}}}}
        last=None
        for i in range(6): last=e.append_fast({'candidate_id':str(i),'risk':.01,'decision_confidence':.8,'network':{'J_C':.1,'mu':.5},'objectives':{'energy':1}},refs)
        self.assertIsNotNone(last['meso']); self.assertIsNotNone(last['slow']); self.assertIsNotNone(last['mrg']); self.assertEqual(e.summary()['meso_count'],2); self.assertEqual(e.summary()['slow_count'],1)

    def test_61_engine_routes_fast_meso_slow_to_kstore(self):
        with tempfile.TemporaryDirectory() as td:
            e=Layer5Engine(cfg(td,fixtures=True,auto=0.0)); r=None
            for _ in range(6): r=e.run(FIX).to_dict()
            self.assertEqual(e.episode.summary()['meso_count'],2); self.assertEqual(e.episode.summary()['slow_count'],1); self.assertTrue(r['experience']['recorded'])

    def test_62_offline_environment_executes_exactly_three_registered_twins(self):
        with tempfile.TemporaryDirectory() as td:
            e=Layer5Engine(cfg(td,fixtures=True,auto=0.0)); calls=[]
            for n in ('WingMPET','MESA_NAV','Climate'): e.register_layer2_twin_adapter(n,lambda a,c,n=n:(calls.append(n) or {'twin':n}))
            r=e.run(FIX,execute_environment=True).to_dict(); self.assertTrue(r['environment_feedback']['executed']); self.assertEqual(sorted(calls),['Climate','MESA_NAV','WingMPET']); self.assertFalse(r['environment_feedback']['layer6_call'])

    def test_63_online_never_starts_new_training_episode(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td,fixtures=True,auto=0.0); k=KStore(c); k.start_episode('E1'); k.append({'x':1}); k.finalize({'ready':True})
            r=Layer5Engine(c).run(FIX,'ONLINE_JSBSIM').to_dict(); self.assertFalse(r['gates']['lifecycle']['new_training_episode']); self.assertFalse(r['gates']['lifecycle']['model_retraining_L5'])

    def test_64_knowledge_TEP_low_coverage_routes_layer7(self):
        c=cfg(); c.data['knowledge']['tep_coverage_min']=0.8
        refs={'TEP':{'twin':'MESA_NAV','id':'T1','version':'v1','hash':'h1','coverage':0.2,'derivatives':{'u_epi':1},'source_layer':'Layer2'}}
        r=KnowledgeReferenceResolver(c).resolve(refs,'sv','sh'); self.assertFalse(r['use_for_normal_decision']); self.assertTrue(r['layer7_evidence']); self.assertFalse(r['raw_rewrite_TEP'])

    def test_65_knowledge_snapshot_incoherence_fails_closed(self):
        refs={'CRP':{'id':'C1','version':'v1','hash':'h1','snapshot_version':'other'}}
        r=KnowledgeReferenceResolver(cfg()).resolve(refs,'sv','sh'); self.assertFalse(r['coherent']); self.assertFalse(r['use_for_normal_decision'])

    def test_66_phoenix_sensor_truth_observation_separated(self):
        s=PhoenixSpecialization(cfg()).sensor_contract(); self.assertTrue(s['truth_observation_separated']); self.assertNotEqual(s['wind_semantics']['sensor_observation'],s['wind_semantics']['environment_truth'])

    def test_67_phoenix_servo_dynamics_fail_closed_without_identification(self):
        r=PhoenixSpecialization(cfg()).predict_servo_step(0,.1,.01); self.assertFalse(r['ok']); self.assertIn('not_identified',r['reason'])

    def test_68_phoenix_transition_schedule_forward_only_without_validation(self):
        p=PhoenixSpecialization(cfg()); r=p.schedule({'airspeed_m_s':1}); self.assertEqual(r['mode'],'FORWARD'); self.assertFalse(r['full_envelope_authorized'])

    def test_69_nonzero_qlpv_correction_requires_chance_certificate(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            d=copy.deepcopy(BASE); d['testing']['allow_fixture_verification']=True; d['phoenix']['mixer']['validated']=True; d['phoenix']['servo_dynamics']['identified']=True; d['phoenix']['servo_dynamics']['tau_s']=[.05,.05]; d['phoenix']['servo_dynamics']['rate_limit_rad_s']=[5.0,5.0]; d['phoenix']['esc_mapping']['identified']=True; d['knowledge']['directory']=str(Path(td)/'kstore')
            cpre=Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml'); ppre=PhoenixSpecialization(cpre); model=Path(td)/'m.npz'
            np.savez(model,A=np.eye(2),B=np.array([[1,0,0,0],[0,1,0,0]],float),calibrated=np.array([1]),phoenix_profile_sha256=np.array([ppre.hash]),airframe_binding_sha256=np.array([ppre.airframe_binding_hash()]))
            d['qlpv']['model_file']=str(model); c0=Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml'); p0=PhoenixSpecialization(c0); c=Layer4ContractAdapter(c0).candidates(FIX)[0]; c.logical_actions=p0.bind_candidate(c.raw)[0]; c.provenance['qlpv_initial_error']=[.2,-.1]
            q=QLPVCorrectionKernel(c0,p0).solve(c); self.assertFalse(q['ok']); self.assertIn('chance_constraint_certificate_failed',q['vp']['reasons'])

    def test_70_base_document_compliance_product_true_on_verified_path(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=0.0)).run(FIX).to_dict(); self.assertTrue(r['compliance']['base_compliance_5']); self.assertTrue(all(r['compliance']['base_document'].values()))

    def test_71_formalism_v4_coverage_flags_true(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=0.0)).run(FIX).to_dict(); self.assertTrue(r['compliance']['implementation_coverage']); self.assertTrue(r['compliance']['formalism_v4']['core_25_25_implemented']); self.assertTrue(r['compliance']['formalism_v4']['Knowledge_v4_12_12_implemented']); self.assertTrue(r['compliance']['formalism_v4']['Phoenix_v4_12_12_implemented'])

    def test_72_offline_confidence_is_marked_non_operational(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=0.0)).run(FIX).to_dict(); self.assertFalse(r['provenance']['offline_confidence_is_operational']); self.assertEqual(r['provenance']['evidence_type'],'offline_simulation_reference')

    def test_73_kstore_interface_and_bounded_ram(self):
        with tempfile.TemporaryDirectory() as td:
            k=KStore(cfg(td)); st=k.status(); self.assertEqual(st['interface'],['Open','Append','Checkpoint','AtomicCommit','Load','Verify','Recover','Compact']); self.assertFalse(st['full_dataset_in_ram'])

    def test_74_finalize_contains_fast_meso_slow_summary(self):
        with tempfile.TemporaryDirectory() as td:
            e=Layer5Engine(cfg(td,fixtures=True,auto=0.0));
            for _ in range(6): e.run(FIX)
            mf=e.finalize_offline_episode(); self.assertTrue(mf['committed']); loaded=KStore(cfg(td)).load_setd(); summary=loaded['knowledge']['knowledge']['episode_summary']; self.assertEqual(summary['meso_count'],2); self.assertEqual(summary['slow_count'],1)

    def test_75_network_model_pwm_invariants_remain_false(self):
        with tempfile.TemporaryDirectory() as td:
            r=Layer5Engine(cfg(td,fixtures=True,auto=0.0)).run(FIX).to_dict(); self.assertFalse(r['network_commit']); self.assertFalse(r['model_retraining']); self.assertFalse(r['direct_pwm']); self.assertTrue(r['provenance']['read_only_network'])

    def test_76_edge_robust_profile_is_default(self):
        p=EdgeProfileManager(cfg()).plan(); self.assertEqual(p['profile'],'EDGE-ROBUST'); self.assertTrue(p['chance_enabled']); self.assertTrue(p['covariance_enabled']); self.assertFalse(p['safety_bounds_relaxed'])

    def test_77_edge_light_reduces_complexity_not_safety(self):
        c=cfg(); c.data['edge']['profile']='EDGE-LIGHT'; p=EdgeProfileManager(c).plan(); self.assertEqual(p['sensitivity_top_k'],0); self.assertFalse(p['safety_bounds_relaxed']); self.assertFalse(p['physical_validation_gates_relaxed'])

    def test_78_edge_safety_enables_cvar(self):
        c=cfg(); c.data['edge']['profile']='EDGE-SAFETY'; p=EdgeProfileManager(c).plan(); self.assertTrue(p['cvar_enabled']); self.assertIn('CVaR',p['features'])

    def test_79_qLPV_airframe_binding_mismatch_fails_closed(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            model=Path(td)/'m.npz'; np.savez(model,A=np.eye(2),B=np.array([[1,0,0,0],[0,1,0,0]],float),calibrated=np.array([1]),phoenix_profile_sha256=np.array(['bad']),airframe_binding_sha256=np.array(['bad']))
            d=copy.deepcopy(BASE); d['qlpv']['model_file']=str(model); d['phoenix']['mixer']['validated']=True; d['phoenix']['servo_dynamics']['identified']=True; d['phoenix']['servo_dynamics']['tau_s']=[.05,.05]; d['phoenix']['servo_dynamics']['rate_limit_rad_s']=[5,5]; d['phoenix']['esc_mapping']['identified']=True; d['knowledge']['directory']=str(Path(td)/'k')
            q=QLPVCorrectionKernel(Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml'),PhoenixSpecialization(Layer5Config(d,ROOT/'configs/configuration_phoenix.yaml'))); self.assertFalse(q.status()['airframe_binding_match']); self.assertFalse(q.status()['nonzero_correction_authorized'])

    def test_80_decision_weights_are_simplex_normalized(self):
        r=PolicyAdapter(cfg()).adapt(FIX); self.assertAlmostEqual(sum(r['decision_weights'].values()),1.0,places=12); self.assertTrue(all(v>=0 for v in r['decision_weights'].values()))

    def test_81_layer4_pareto_front_filters_registry_input(self):
        d=copy.deepcopy(FIX); d['pareto_front']=[{'candidate_id':d['candidates'][0]['candidate_id']}]; cs=Layer4ContractAdapter(cfg(fixtures=True)).candidates(d); self.assertEqual(len(cs),1); self.assertEqual(cs[0].candidate_id,d['candidates'][0]['candidate_id'])

    def test_82_layer6_formal_contract_structure_is_not_native_authority(self):
        from layer5.models import Layer6ActionPackage
        p=Layer6ActionPackage(command=[0.0,0.0,0.0,0.0],actuator_targets=['aileron_domain','elevator_domain','throttle_domain','rudder_domain'],
            execution_constraints={},safety_policy={},apply_time=1.0,deadline_time=1.1,decision_confidence=0.0,
            provenance={'test_fixture':True,'flight_authorized':False},version='v').to_dict()
        for k0 in ['u_cmd','A_target','C_exec','S_safe','tau_apply','tau_deadline','rho_decision','pi_decision','nu']:
            self.assertIn(k0,p['formal_contract'])
        self.assertEqual(p['Source'],'Layer5')
        with self.assertRaisesRegex(RuntimeError,'native_pilot_controller_prediction_unavailable'):
            Layer6Assembler(cfg()).assemble([0,0,0,0],p['actuator_targets'],{}, {},1.0,1.1,0.0,{'native_controller_certified':True})

    def test_83_TEP_requires_Layer2_provenance(self):
        refs={'TEP':{'twin':'MESA_NAV','id':'T1','version':'v1','hash':'h1','coverage':1.0,'derivatives':{'u_epi':1}}}
        r=KnowledgeReferenceResolver(cfg()).resolve(refs,'sv','sh'); self.assertFalse(r['ok']); self.assertIn('not_provenanced_from_Layer2',str(r['errors']))

    def test_84_epistemic_weights_are_separate_read_only_from_decision_weights(self):
        r=PolicyAdapter(cfg()).adapt(REAL); self.assertTrue(r['W_epi_not_beta_dec']); self.assertIsInstance(r['epistemic_weights_read_only'],dict); self.assertAlmostEqual(sum(r['decision_weights'].values()),1.0)

    def test_85_empty_offline_episode_cannot_finalize(self):
        with tempfile.TemporaryDirectory() as td:
            k=KStore(cfg(td)); k.start_episode('E1')
            with self.assertRaises(RuntimeError): k.finalize({})


    def test_86_latest_mrg_is_consumed_by_next_engine_cycle(self):
        with tempfile.TemporaryDirectory() as td:
            e=Layer5Engine(cfg(td,fixtures=True,auto=0.0))
            e.episode.mrg_history.append({"kind":"MRG","objective_weights":{"risk":9.0},"read_only":True})
            r=e.run(FIX).to_dict()
            self.assertTrue(r['policy']['episode_mrg_used'])
            self.assertTrue(r['policy']['gamma']['source_episode_mrg'])
            self.assertTrue(any(x.get('source')=='meso' and x.get('objective')=='risk' for x in r['policy']['applied_weight_updates']))

    def test_87_verified_setd_prior_is_consumed_online_without_retraining(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td,fixtures=True,auto=0.0)
            k=KStore(c); k.start_episode('E1'); k.append({'x':1})
            k.finalize({'decision_weights':{'risk':9.0,'energy':1.0},'ready':True,'knowledge_quality':{'safety_knowledge_valid':True,'control_knowledge_valid':True,'verified_candidate_cycles':1,'final_authorized_cycles':1,'closed_loop_effect_cycles':1,'full_fidelity_runtime_error_cycles':0}})
            r=Layer5Engine(c).run(FIX,'ONLINE_JSBSIM').to_dict()
            self.assertTrue(r['knowledge']['setd']['ok'])
            self.assertTrue(r['policy']['setd_prior_used'])
            self.assertFalse(r['model_retraining'])
            self.assertFalse(r['gates']['lifecycle']['new_training_episode'])
            self.assertGreater(r['policy']['decision_weights']['risk'], r['policy']['decision_weights']['energy'])

    def test_88_tddt_kstore_root_environment_overrides_yaml(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as envtd:
            old=os.environ.get('TDDT_KSTORE_ROOT'); os.environ['TDDT_KSTORE_ROOT']=envtd
            try:
                k=KStore(cfg(td)); self.assertEqual(Path(k.status()['root']),Path(envtd).resolve()); self.assertEqual(k.status()['root_source'],'TDDT_KSTORE_ROOT')
            finally:
                if old is None: os.environ.pop('TDDT_KSTORE_ROOT',None)
                else: os.environ['TDDT_KSTORE_ROOT']=old

    def test_89_file_backend_is_segmented_without_monolithic_stream(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); c.data['knowledge']['file_segment_records']=2; k=KStore(c); k.start_episode('E1')
            for i in range(5): k.append({'i':i})
            root=Path(td)/'kstore'; segs=sorted((root/'ss'/'segments').glob('segment_*.jsonl'))
            self.assertEqual(len(segs),3); self.assertFalse((root/'ss_kstore.jsonl').exists()); self.assertTrue(all(len(x.read_text().splitlines())<=2 for x in segs)); self.assertFalse(k.status()['monolithic_ss_stream'])

    def test_90_ss_manifest_hashes_and_record_chain_verify(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); c.data['knowledge']['file_segment_records']=2; k=KStore(c); k.start_episode('E1')
            for i in range(5): k.append({'i':i})
            v=k.verify_ss(); self.assertTrue(v['ok']); self.assertEqual(v['records'],5); self.assertEqual(v['segments'],3)
            m=json.loads((Path(td)/'kstore'/'ss'/'SS_MANIFEST.json').read_text()); self.assertEqual(m['schema'],'SS-KStore-manifest-v2'); self.assertTrue(all(x.get('sha256') for x in m['segments']))

    def test_91_setd_is_multi_file_sharded_and_loadable(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); c.data['knowledge']['setd_component_items']=25; k=KStore(c); k.start_episode('E1')
            for i in range(4): k.append({'i':i})
            big={'decision_weights':{'risk':.7,'energy':.3},'MRG_history':[{'i':i,'risk':i/100.0} for i in range(130)],'episode_summary':{'fast_count':4},'ready':True}
            mf=k.finalize(big); root=Path(td)/'kstore'; ptr=json.loads((root/'SETD_KSTORE.json').read_text()); self.assertEqual(ptr['schema'],'SETD-KStore-pointer-v2'); self.assertLess((root/'SETD_KSTORE.json').stat().st_size,4096)
            pkg=(root/ptr['manifest_file']).parent; pm=json.loads((pkg/'manifest.json').read_text()); self.assertEqual(pm['layout'],'multi-file-sharded'); self.assertFalse(pm['raw_training_trajectory_in_setd']); self.assertGreater(len(list((pkg/'components'/'MRG_history').glob('part_*.json'))),1)
            loaded=KStore(c).load_setd(); self.assertTrue(loaded['ok']); self.assertEqual(loaded['knowledge']['knowledge']['MRG_history'],big['MRG_history']); self.assertEqual(mf['layout'],'segmented-ss+multi-file-setd-v2')

    def test_92_incomplete_setd_staging_is_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); k=KStore(c); st=Path(td)/'kstore'/'setd'/'staging'/'broken.stage'; st.mkdir(parents=True); (st/'x').write_text('partial')
            k2=KStore(c); self.assertFalse(st.exists()); q=list((Path(td)/'kstore'/'quarantine').glob('broken.stage.incomplete_setd_staging.*')); self.assertTrue(q)

    def test_93_preflight_reports_segmented_kstore_and_env_contract(self):
        with tempfile.TemporaryDirectory() as td:
            e=Layer5Engine(cfg(td)); r=e.preflight(False); ks=r['knowledge_store']; self.assertEqual(ks['environment_variable'],'TDDT_KSTORE_ROOT'); self.assertTrue(ks['setd_multi_file']); self.assertFalse(ks['monolithic_ss_stream']); self.assertTrue(r['components']['KStore_segmented_FILE'])

    def test_94_kstore_schema_resources_are_shipped(self):
        for name in ['kstore_record_schema.json','ss_kstore_manifest_schema.json','setd_package_manifest_schema.json']:
            d=json.loads((ROOT/'resources'/name).read_text()); self.assertIn('$schema',d)

    def test_95_setd_pointer_tamper_recovers_from_committed_package(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); k=KStore(c); k.start_episode('E1'); k.append({'x':1}); k.finalize({'ready':True}); root=Path(td)/'kstore'; (root/'SETD_KSTORE.json').write_text('{}')
            r=KStore(c).load_setd(); self.assertTrue(r['ok']); self.assertTrue(r['recovery']['performed']); self.assertEqual(r['recovery']['action'],'load_latest_committed_verified')

    def test_96_ss_single_record_over_byte_budget_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); c.data['knowledge']['file_segment_max_bytes']=4096; c.data['knowledge']['file_record_max_bytes']=2048
            k=KStore(c); k.start_episode('E1')
            with self.assertRaises(RuntimeError): k.append({'blob':'x'*10000})
            self.assertTrue(k.status()['critical_loss'])
            self.assertEqual(len(list((Path(td)/'kstore'/'ss'/'segments').glob('segment_*.jsonl'))),0)

    def test_97_setd_oversized_scalar_is_raw_byte_sharded_and_reconstructed(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); c.data['knowledge']['setd_component_max_bytes']=4096
            k=KStore(c); k.start_episode('E1'); k.append({'x':1})
            blob='αβγδ'*7000
            k.finalize({'oversized_scalar':blob,'ready':True})
            root=Path(td)/'kstore'; ptr=json.loads((root/'SETD_KSTORE.json').read_text()); pkg=(root/ptr['manifest_file']).parent
            pm=json.loads((pkg/'manifest.json').read_text()); desc=next(x for x in pm['components'] if x['key']=='oversized_scalar')
            self.assertEqual(desc['kind'],'json_bytes_sharded'); self.assertGreater(len(desc['files']),1)
            self.assertTrue(all(int(f['bytes'])<=4096 for f in desc['files']))
            self.assertEqual(KStore(c).load_setd()['knowledge']['knowledge']['oversized_scalar'],blob)

    def test_98_setd_list_chunks_obey_item_and_byte_budgets(self):
        with tempfile.TemporaryDirectory() as td:
            c=cfg(td); c.data['knowledge']['setd_component_items']=1000; c.data['knowledge']['setd_component_max_bytes']=4096
            k=KStore(c); k.start_episode('E1'); k.append({'x':1})
            values=[{'i':i,'payload':'z'*350} for i in range(80)]
            k.finalize({'values':values})
            root=Path(td)/'kstore'; ptr=json.loads((root/'SETD_KSTORE.json').read_text()); pkg=(root/ptr['manifest_file']).parent
            pm=json.loads((pkg/'manifest.json').read_text()); desc=next(x for x in pm['components'] if x['key']=='values')
            self.assertIn(desc['kind'],{'list_sharded','json_bytes_sharded'}); self.assertGreater(len(desc['files']),1)
            self.assertTrue(all(int(f['bytes'])<=4096 for f in desc['files']))
            self.assertEqual(KStore(c).load_setd()['knowledge']['knowledge']['values'],values)

    def test_99_ptlib_not_vendored_and_env_is_only_declared_root(self):
        self.assertFalse((ROOT/'pt_lib').exists())
        self.assertFalse((ROOT/'vendor'/'pt_lib').exists())
        self.assertFalse(any(p.is_file() and 'pt_lib' in p.name.lower() and (p.suffix in {'.gz','.tar','.zip'} or '.tar.' in p.name.lower()) for p in ROOT.rglob('*')))
        env=(ROOT/'configs/environment.sh').read_text()
        self.assertIn('export TDDT_PTLIB_ROOT="${TDDT_PTLIB_ROOT:-}"',env)
        self.assertNotIn('$TDDT_PTLIB_ROOT', env.split('export PYTHONPATH=',1)[1] if 'export PYTHONPATH=' in env else '')

    def test_100_ptlib_incomplete_root_never_falls_back_to_global_import(self):
        old=os.environ.get('TDDT_PTLIB_ROOT')
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ['TDDT_PTLIB_ROOT']=td
                a=PTLibJSBSimAdapter(cfg())
                with self.assertRaisesRegex(RuntimeError,'invalid/incomplete'):
                    a._load()
        finally:
            if old is None: os.environ.pop('TDDT_PTLIB_ROOT',None)
            else: os.environ['TDDT_PTLIB_ROOT']=old

    def test_101_offline_hold_is_recorded_and_can_finalize_setd(self):
        with tempfile.TemporaryDirectory() as td:
            e=Layer5Engine(cfg(td,fixtures=False))
            r=e.run(REAL,'OFFLINE_SE').to_dict()
            self.assertTrue(r['fallback']['used'])
            self.assertTrue(r['experience']['recorded'])
            self.assertTrue(r['experience']['hold'])
            self.assertIsNone(r['control_action'])
            self.assertFalse(r['environment_feedback']['executed'])
            mf=e.finalize_offline_episode({'test':'hold_only_episode'})
            self.assertTrue(mf['committed'])
            loaded=KStore(cfg(td)).load_setd()
            self.assertTrue(loaded['ok'])
            self.assertGreaterEqual(loaded['knowledge']['knowledge']['episode_summary']['fast_count'],1)

    def test_102_ptlib_explicit_prepare_connect_failures_do_not_latch_ready(self):
        class BadLifecycle:
            def prepare(self): return {'ok':False,'status':'NOT_READY'}
        class BadController:
            def connect_session(self): return {'ok':False,'connected':False}
        a=PTLibJSBSimAdapter(cfg()); a.lifecycle=BadLifecycle()
        with self.assertRaisesRegex(RuntimeError,'prepare did not establish'):
            a.prepare()
        self.assertFalse(a.prepared)
        b=PTLibJSBSimAdapter(cfg()); b.controller=BadController()
        with self.assertRaisesRegex(RuntimeError,'connect did not establish'):
            b.connect()
        self.assertFalse(b.connected)

    def test_103_commission_certificate_enables_nominal_tracker_but_not_nonzero_correction(self):
        ph=PhoenixSpecialization(cfg())
        cert={
            'schema':'PTLIB-Phoenix-control-commission-v1','ok':True,'sitl_only':True,'physics_held_during_commissioning':True,
            'mixer':{'validated':True,'elevon_matrix_rad_per_norm':[[0.30,0.20],[-0.30,0.20]],'trim_rad':[0.05,0.05]},
            'trim':{'ready':True,'pitch_request_norm':0.44,'throttle_request_norm':0.35},
            'throttle_path':{'validated':True},'nominal_tracker_authorized':True,
            'servo_dynamics':{'identified':False,'measured':False},'esc_mapping':{'identified':False,'measured':False}
        }
        a=ph.apply_runtime_validation_certificate(cert)
        self.assertTrue(a['ok']); gates=ph.correction_validation_gates()
        self.assertTrue(gates['nominal_control']); self.assertFalse(gates['qlpv_nonzero_correction'])
        self.assertFalse(gates['servo_dynamics']); self.assertFalse(gates['esc_mapping'])

    def test_104_layer4_trajectory_can_bind_nominal_pilot_controls_without_5d_action_mapping(self):
        ph=PhoenixSpecialization(cfg())
        cert={
            'schema':'PTLIB-Phoenix-control-commission-v1','ok':True,'sitl_only':True,'physics_held_during_commissioning':True,
            'mixer':{'validated':True,'elevon_matrix_rad_per_norm':[[0.30,0.20],[-0.30,0.20]],'trim_rad':[0.05,0.05]},
            'trim':{'ready':True,'pitch_request_norm':0.40,'throttle_request_norm':0.34},
            'throttle_path':{'validated':True},'nominal_tracker_authorized':True
        }
        self.assertTrue(ph.apply_runtime_validation_certificate(cert)['ok'])
        # Layer-4 state order: pNED(3), VgNED(3), VaNED(3), airspeed, gamma, heading, ...
        x0=[0,0,0, 0,0,0, 13,0,0, 13,0,0] + [0]*7
        x1=[20,2,0, 0,0,0, 13,0,0, 13,0.02,0.05] + [0]*7
        x2=[40,4,0, 0,0,0, 13,0,0, 13,0.01,0.08] + [0]*7
        cand={'trajectory':[x0,x1,x2], 'actions':[[9,8,7,6,5],[4,3,2,1,0]]}
        seq,rec=ph.bind_candidate(cand)
        self.assertTrue(rec['ok']); self.assertEqual(rec['source'],'commissioned_layer4_trajectory_reference_tracker')
        self.assertEqual(len(seq),2); self.assertTrue(all(len(u)==4 for u in seq)); self.assertTrue(all(u[3]==0.0 for u in seq))
        self.assertIn('no 5D abstract-action mapping',rec['mapping_semantics'])

    def test_103_demo_aligned_control_preparation_is_cached_across_control_steps(self):
        a=object.__new__(PTLibJSBSimAdapter)
        a.controller=None; a.lifecycle=None; a.prepared=True; a.connected=True; a.control_preparation=None; a.external_control_ready=None
        class C:
            def __init__(self): self.calls=0
            def external_control_prepare_session(self,background_poll=True): self.calls+=1; return {"ok":True,"ready":True,"sequence":["prepare","connect","precheck"]}
        c=C(); a.controller=c
        r1=a.prepare_control_session(); r2=a.ensure_external_control_ready(); r3=a.ensure_external_control_ready()
        self.assertTrue(r1['ready'] and r2['ready'] and r3['ready']); self.assertEqual(c.calls,1)

    def test_104_commissioning_never_runs_before_demo_aligned_preparation(self):
        a=object.__new__(PTLibJSBSimAdapter)
        a.controller=None; a.lifecycle=None; a.prepared=True; a.connected=True; a.control_preparation=None; a.external_control_ready=None
        class C:
            def __init__(self): self.commission=0
            def external_control_prepare_session(self,background_poll=True): return {"ok":False,"ready":False,"reason":"precheck_not_ready"}
            def phoenix_control_commission_session(self,background_poll=True): self.commission+=1; return {"ok":True}
        c=C(); a.controller=c; out=a.commission_control_path()
        self.assertFalse(out['ok']); self.assertEqual(c.commission,0); self.assertEqual(out['reason'],'ptlib_demo_aligned_control_preparation_failed')

    def test_105_ptlib_0932_fmu_root_and_boulder_binding_are_hard_gates(self):
        if PTLIB is None or not PTLIB.exists(): self.skipTest('external PT_LIB root not configured')
        old_fmu=os.environ.get('EXT_FMU_SIM_ROOT'); old_bo=os.environ.get('TDDT_BOULDER_ROOT')
        try:
            fmu=Path(os.environ.get('EXT_FMU_SIM_ROOT',''))
            bo=Path(os.environ.get('TDDT_BOULDER_ROOT',''))
            if not fmu.is_dir() or not bo.is_dir(): self.skipTest('FMI/Boulder integration roots not configured')
            a=PTLibJSBSimAdapter(cfg()); r=a.probe()
            self.assertTrue(r['ok']); self.assertTrue(r['fmu']['ok']); self.assertTrue(r['resource_binding']['ok'])
            self.assertEqual(r['fmu']['environment_variable'],'EXT_FMU_SIM_ROOT')
            self.assertTrue(all(x['byte_identical'] for x in r['resource_binding']['assets']))
        finally:
            if old_fmu is None: os.environ.pop('EXT_FMU_SIM_ROOT',None)
            else: os.environ['EXT_FMU_SIM_ROOT']=old_fmu
            if old_bo is None: os.environ.pop('TDDT_BOULDER_ROOT',None)
            else: os.environ['TDDT_BOULDER_ROOT']=old_bo

    def test_106_offline_route_does_not_construct_ptlib_fmi_client(self):
        with tempfile.TemporaryDirectory() as td:
            e=Layer5Engine(cfg(td,fixtures=True,auto=.60))
            self.assertIsNone(e.online_env)
            r=e.run(FIX,'OFFLINE_SE').to_dict()
            self.assertFalse(r['provenance']['environment_route']['layer6_call']); self.assertIsNone(e.online_env)

if __name__=='__main__': unittest.main()


def test_online_commission_mixer_validates_physical_nominal_envelope_not_raw_gain(tmp_path):
    from layer5 import Layer5Config
    from layer5.phoenix import PhoenixSpecialization
    cfg=Layer5Config.from_yaml(Path(__file__).parents[1]/"configs/configuration_phoenix.yaml")
    ph=PhoenixSpecialization(cfg)
    cert={
      "schema":"PTLIB-Phoenix-control-commission-v1","ok":True,"sitl_only":True,"physics_held_during_commissioning":True,
      "mixer":{"validated":True,"elevon_matrix_rad_per_norm":[[-0.6139166666666668,0.2738333333333334],[0.6183333333333333,0.1766666666666667]],"trim_rad":[0.0689,0.0689]},
      "trim":{"ready":True,"pitch_request_norm":0.27,"throttle_request_norm":0.3402},
      "throttle_path":{"validated":True},"nominal_tracker_authorized":True,"direct_pwm":False,
      "servo_dynamics":{"identified":False,"measured":False},"esc_mapping":{"identified":False,"measured":False},
    }
    out=ph.apply_runtime_validation_certificate(cert)
    assert out["ok"] and out["accepted"] and out["online_ready"]
    # Increasing the nominal envelope makes the same measured mixer unsafe and must fail closed.
    cfg.data["phoenix"]["nominal_tracking"]["max_aileron_norm"]=1.0
    cfg.data["phoenix"]["nominal_tracking"]["max_elevator_norm"]=1.0
    ph2=PhoenixSpecialization(cfg); bad=ph2.apply_runtime_validation_certificate(cert)
    assert bad["ok"] is False and "mixer_nominal_envelope_exceeds_elevon_limit" in bad["errors"]


def test_ptlib_base_safe_control_write_uses_public_fmi_client():
    from layer5.environment import PTLibJSBSimAdapter
    a=object.__new__(PTLibJSBSimAdapter)
    a.controller=object(); a.connected=True
    a.ensure_external_control_ready=lambda:{'ok':True,'ready':True}
    seen={}
    class Client:
        def send_pilot_command(self,**kw): seen.update(kw); return {'running':True,'public':True}
    a.client=Client()
    out=PTLibJSBSimAdapter.safe_control_write(a,[0.1,-0.2,0.55,0.0],decision_id='D1',transaction_id='T1',rate_hz=10.0,ttl_s=8.0)
    assert out['public_api_used'] is True and out['direct_pwm'] is False
    assert seen=={'aileron':0.1,'elevator':-0.2,'throttle':0.55,'rudder':0.0,'decision_id':None,'transaction_id':None,'rate_hz':None,'ttl_s':None,'execution_authorization':None}


def test_ptlib_online_snapshot_uses_nonblocking_status_but_preserves_health_result():
    a=object.__new__(PTLibJSBSimAdapter)
    a.controller=object(); a.connected=True
    seen=[]
    class Client:
        def status(self, *, observation_s):
            seen.append(float(observation_s))
            return {'io':{'ready':True},'fmi':{'ready':True,'checks_preserved':True}}
        def read_state(self): return {'sim_time_s':1.0}
        def read_actuator_feedback(self): return {'left_motor_norm':0.5}
    a.client=Client()
    out=a.io_snapshot()
    assert seen==[0.0]
    assert out['ready'] is True
    assert out['fmi_health']=={'ready':True,'checks_preserved':True}


def test_ptlib_rate_sync_bridge_propagates_public_command_metadata():
    from layer5.environment import PTLibJSBSimAdapter
    a=object.__new__(PTLibJSBSimAdapter); a.controller=object(); a.connected=True; a.ensure_external_control_ready=lambda:{'ok':True,'ready':True}
    seen={}
    class Client:
        def send_pilot_command(self,**kw):
            seen.update(kw)
            return {'running':True,'send_count':1,'last_result':{'release_confirmation':{'ready':True,'released':True,'holding':False}}}
    a.client=Client(); a.configure_tdoc_rate_sync({'enabled':True,'decision_id':'D1','transaction_id':'T1','ptlib_control_hz':10.0,'command_ttl_s':8.0,'release_on_stale':True})
    out=a.safe_control_write([0.1,-0.2,0.55,0.0])
    assert out['public_api_used'] is True and out['release_confirmed'] is True
    assert seen=={'aileron':0.1,'elevator':-0.2,'throttle':0.55,'rudder':0.0,'decision_id':'D1','transaction_id':'T1','rate_hz':10.0,'ttl_s':8.0,'execution_authorization':None}


def test_solution1_finalize_never_upgrades_explicit_tdoc_false():
    with tempfile.TemporaryDirectory() as td:
        e=Layer5Engine(cfg(td,fixtures=False))
        e.run(REAL,'OFFLINE_SE')
        e.knowledge_quality_counts.update({"verified_candidate_cycles":9,"final_authorized_cycles":9,"closed_loop_effect_cycles":9,"full_fidelity_runtime_error_cycles":0})
        q={
            "control_knowledge_valid":False,
            "phase_control_coverage":{"all_required_control_phases":False,"missing_control_phases":["vertical_takeoff","hover"]},
            "verified_candidate_cycles":9,"final_authorized_cycles":9,"closed_loop_effect_cycles":9,"full_fidelity_runtime_error_cycles":0,
        }
        out=e.finalize_offline_episode({"knowledge_quality":q})
        assert out["committed"] is True
        assert out["knowledge_quality"]["control_knowledge_valid"] is False
        assert out["knowledge_quality"]["control_knowledge_valid_inputs"]["supplied_tdoc_control_valid"] is False
        assert out["knowledge_quality"]["control_knowledge_valid_inputs"]["all_required_control_phases"] is False

def test_solution1_finalize_accepts_only_complete_supplied_phase_quality():
    with tempfile.TemporaryDirectory() as td:
        e=Layer5Engine(cfg(td,fixtures=False))
        e.run(REAL,'OFFLINE_SE')
        e.knowledge_quality_counts.update({"verified_candidate_cycles":9,"final_authorized_cycles":9,"closed_loop_effect_cycles":9,"full_fidelity_runtime_error_cycles":0})
        q={
            "control_knowledge_valid":True,
            "phase_control_coverage":{"all_required_control_phases":True,"missing_control_phases":[]},
            "verified_candidate_cycles":9,"final_authorized_cycles":9,"closed_loop_effect_cycles":9,"full_fidelity_runtime_error_cycles":0,
        }
        out=e.finalize_offline_episode({"knowledge_quality":q})
        assert out["committed"] is True
        assert out["knowledge_quality"]["control_knowledge_valid"] is True
