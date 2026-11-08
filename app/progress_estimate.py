"""Conservative ETA for mixed cached/LLM work.

Never extrapolate a few expensive requests across a whole document containing
mostly deterministic tables. If model latency/mixture is unstable, return None
instead of showing a false clock. Keeps only transient samples, no credentials.
"""
from collections import deque
from statistics import median


class DiscoveryETA:
    def __init__(self, parallelism=1):
        self.parallelism=max(1, int(parallelism))
        self.observed=0
        self.model_units=0
        self.model_seconds=deque(maxlen=24)
        self.previous=None

    def observe(self, units, seconds, *, model_invoked=False, cache_hit=False):
        units=max(1, int(units))
        self.observed+=units
        if model_invoked and not cache_hit and seconds>=0:
            self.model_units+=units
            self.model_seconds.append(float(seconds)/units)

    def estimate(self, done, total):
        """Return seconds only with a stable, representative observed sample."""
        if not total or done>=total or done<max(16,round(total*0.15)):
            return None
        values=list(self.model_seconds)
        if len(values)<6 or self.model_units<6:
            return None
        mid=median(values)
        if mid<=0:return None
        # Median relative absolute deviation catches heterogeneous long calls.
        deviation=median(abs(x-mid) for x in values)/mid
        if deviation>0.55 or max(values)>mid*4:
            self.previous=None
            return None
        fraction=self.model_units/max(1,self.observed)
        if fraction<=0:return None
        # Divide only by actual bounded concurrent workers, not all configured
        # API threads or arbitrary model throughput assumptions.
        seconds=round((total-done)*fraction*mid/self.parallelism)
        if seconds<1:return None
        if self.previous is not None and (seconds>self.previous*1.6 or seconds<self.previous*0.35):
            # The change itself demonstrates the estimate is unreliable.
            self.previous=None
            return None
        self.previous=seconds
        return seconds
