"""
event_stream.py — append-only event sourcing with deterministic replay.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class EventRecord:
    event_type: str
    trace_id: str
    timestamp: float
    payload: Dict[str, Any]


class EventStream:
    """
    Simple append-only JSONL event stream for Proof Vault replay.

    The stream is deliberately filesystem-native and dependency-light so it can
    be mirrored into WORM/object storage or tailed by external policy engines.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, trace_id: str, payload: Optional[Dict[str, Any]] = None) -> EventRecord:
        record = EventRecord(
            event_type=event_type,
            trace_id=trace_id,
            timestamp=time.time(),
            payload=payload or {},
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        return record

    def read(self, trace_id: Optional[str] = None) -> List[EventRecord]:
        events: List[EventRecord] = []
        if not self.path.exists():
            return events
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                data = json.loads(line)
                if trace_id and data["trace_id"] != trace_id:
                    continue
                events.append(EventRecord(**data))
        return events

    def replay(self, trace_id: str) -> Dict[str, Any]:
        events = self.read(trace_id)
        state: Dict[str, Any] = {
            "trace_id": trace_id,
            "created": False,
            "objective": None,
            "meta": {},
            "steps": [],
        }
        for event in events:
            if event.event_type == "trace.created":
                state["created"] = True
                state["objective"] = event.payload.get("objective")
                state["meta"] = event.payload.get("meta", {})
            elif event.event_type == "step.appended":
                state["steps"].append(event.payload)
        return state
