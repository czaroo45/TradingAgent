"""Minimal dependency-free health state for service and data pipelines."""
from __future__ import annotations

import threading
import time


class ServiceHealth:
    def __init__(self):
        self._lock=threading.Lock();self._last_success:dict[str,float]={};self._errors:dict[str,int]={};self._status:dict[str,str]={}

    def success(self, service: str) -> None:
        with self._lock:self._last_success[service]=time.time();self._status[service]="ok"

    def failure(self, service: str) -> None:
        with self._lock:self._errors[service]=self._errors.get(service,0)+1;self._status[service]="degraded"

    def snapshot(self, stale_after_seconds: float=300) -> dict:
        now=time.time()
        with self._lock:
            services={}
            for name in sorted(set(self._status)|set(self._last_success)|set(self._errors)):
                last=self._last_success.get(name)
                stale=last is None or now-last>stale_after_seconds
                services[name]={"status":"stale" if stale else self._status.get(name,"unknown"),
                                "last_success_epoch":last,"errors":self._errors.get(name,0)}
            return {"status":"ok" if all(x["status"]=="ok" for x in services.values()) else "degraded",
                    "services":services}


__all__=["ServiceHealth"]
