from __future__ import annotations
import os, resource, threading, time


def process_peak_rss_mb()->float:
    """Lifetime process peak RSS; diagnostic only, never used as Layer-4 budget usage."""
    r=float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Linux reports KiB; macOS reports bytes. Edge target is Linux, but keep portable.
    if r>10_000_000:
        return r/(1024.0*1024.0)
    return r/1024.0


def current_rss_mb()->float:
    """Current process RSS in MiB, preferring Linux /proc for allocation-neutral sampling."""
    try:
        with open('/proc/self/statm','r',encoding='ascii') as f:
            resident_pages=int(f.read().split()[1])
        return resident_pages*float(os.sysconf('SC_PAGE_SIZE'))/(1024.0*1024.0)
    except Exception:
        # Portable fallback cannot provide current RSS; lifetime peak is conservative.
        return process_peak_rss_mb()


class InvocationMemoryProbe:
    """Bounded low-overhead sampler for per-invocation Layer-4 working-set growth.

    The edge memory budget M_L4 is an invocation workload budget, not a limit on the
    resident memory of the entire TDOC process.  Sampling absolute lifetime ru_maxrss
    made C_bounded permanently false after any previous process peak.  This probe
    records the process RSS at Layer-4 entry and the maximum current RSS observed while
    that invocation is executing; only the positive increment is charged to Layer 4.
    """
    def __init__(self, interval_ms:float=10.0):
        self.interval_s=max(float(interval_ms),1.0)/1000.0
        self.baseline_rss_mb=current_rss_mb()
        self.peak_process_rss_mb=self.baseline_rss_mb
        self._stop=threading.Event()
        self._thread=threading.Thread(target=self._sample_loop,name='layer4-memory-probe',daemon=True)
        self._thread.start()

    def _sample_loop(self):
        while not self._stop.wait(self.interval_s):
            try:
                self.peak_process_rss_mb=max(self.peak_process_rss_mb,current_rss_mb())
            except Exception:
                pass

    def stop(self)->dict:
        self._stop.set()
        self._thread.join(timeout=max(0.05,self.interval_s*3.0))
        end=current_rss_mb()
        self.peak_process_rss_mb=max(self.peak_process_rss_mb,end)
        return {
            'baseline_process_rss_mb':float(self.baseline_rss_mb),
            'peak_process_rss_mb':float(self.peak_process_rss_mb),
            'end_process_rss_mb':float(end),
            'invocation_peak_increment_mb':float(max(0.0,self.peak_process_rss_mb-self.baseline_rss_mb)),
            'invocation_retained_delta_mb':float(max(0.0,end-self.baseline_rss_mb)),
            'sampling_interval_ms':float(self.interval_s*1000.0),
            'measurement_scope':'Layer4 invocation incremental process working-set; whole-process RSS retained as diagnostic only',
        }


def evaluate_budget(config,elapsed_s:float,memory:dict|None=None)->dict:
    cpu_ms=float(elapsed_s)*1000.0; cpu_budget=float(config.get('edge.cpu_budget_ms',850.0))
    mem_budget=float(config.get('edge.memory_budget_mb',192.0))
    if memory is None:
        # Stand-alone callers that do not install a probe remain fail-conservative and
        # explicit about the measurement scope.
        abs_rss=current_rss_mb()
        memory={'baseline_process_rss_mb':abs_rss,'peak_process_rss_mb':abs_rss,'end_process_rss_mb':abs_rss,
                'invocation_peak_increment_mb':abs_rss,'invocation_retained_delta_mb':0.0,
                'sampling_interval_ms':None,'measurement_scope':'fallback current process RSS; no invocation probe'}
    mem=float(memory.get('invocation_peak_increment_mb',0.0))
    power=float(config.get('edge.estimated_cpu_power_w',8.0)); energy=float(elapsed_s)*power; energy_budget=float(config.get('edge.energy_budget_j',50.0))
    return {'elapsed_s':float(elapsed_s),'cpu_ms':cpu_ms,'cpu_budget_ms':cpu_budget,'within_cpu_budget':cpu_ms<=cpu_budget,
            # peak_rss_mb remains for report compatibility, but now means the peak
            # incremental working set charged to this Layer-4 invocation.
            'peak_rss_mb':mem,'memory_usage_mb':mem,'memory_budget_mb':mem_budget,'within_memory_budget':mem<=mem_budget,
            **memory,
            'energy_estimated_j':energy,'estimated_cpu_power_w':power,'energy_budget_j':energy_budget,'within_energy_budget':energy<=energy_budget,
            'energy_measurement':'estimated from configured CPU power; replace with board power telemetry when available',
            'all_within_budget':bool(cpu_ms<=cpu_budget and mem<=mem_budget and energy<=energy_budget)}
