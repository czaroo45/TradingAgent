"""Safe structured JSON logging and process-local operational counters."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

_SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password)(\s*[:=]\s*)[^\s,;&]+")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = _SECRET.sub(r"\1\2[REDACTED]", record.getMessage())
        payload = {"timestamp":datetime.now(timezone.utc).isoformat(),"level":record.levelname,
                   "logger":record.name,"message":message}
        for key in ("symbol","timeframe","event","status","duration_ms"):
            value=getattr(record,key,None)
            if value is not None:payload[key]=value
        return json.dumps(payload,ensure_ascii=False,default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler=logging.StreamHandler();handler.setFormatter(JsonFormatter())
    root=logging.getLogger();root.handlers.clear();root.addHandler(handler);root.setLevel(level)
