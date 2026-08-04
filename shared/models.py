"""
Shared data models for lab-monitor

Message format:
    Every message has a standard header (device identity + timestamp) and a
    data payload (arbitrary key-value dict with an optional 'data_type' label).

    Single message:
        {
            "header": {
                "device_name": "Triton",
                "device_id":   "synology-triton",
                "device_type": "synology",
                "timestamp":   "2026-08-04T12:00:00Z"
            },
            "data": {
                "data_type": "folder_usage",   # optional
                "folders":   [...]
            }
        }

    Queue payload (collector → manager):
        {
            "queue_id": "Triton-2026-08-04-12-00-00",
            "name":     "Triton",
            "id":       "synology-triton",
            "messages": [ <message>, <message>, ... ]
        }
"""
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
import json


@dataclass
class MessageHeader:
    """Standard header present in every message."""
    device_name: str
    device_id:   str
    device_type: str
    timestamp:   str  # ISO 8601, e.g. "2026-08-04T12:00:00Z"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MessageHeader":
        return MessageHeader(
            device_name=d["device_name"],
            device_id=d["device_id"],
            device_type=d.get("device_type", "unknown"),
            timestamp=d["timestamp"],
        )


@dataclass
class Message:
    """
    Single message: header + data payload.

    The data dict may contain an optional 'data_type' key that tells the
    manager how to store and interpret the remaining key-value pairs.
    If 'data_type' is absent the manager stores data in the 'not_specified'
    table using generic flat key-value storage.
    """
    header: MessageHeader
    data:   Dict[str, Any]

    @property
    def data_type(self) -> Optional[str]:
        return self.data.get("data_type")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "header": self.header.to_dict(),
            "data":   self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Message":
        return Message(
            header=MessageHeader.from_dict(d["header"]),
            data=d.get("data", {}),
        )

    @staticmethod
    def from_json(s: str) -> "Message":
        return Message.from_dict(json.loads(s))


@dataclass
class QueuePayload:
    """
    Batch of messages posted from one collector to the manager.

    queue_id is a unique identifier for this batch.  The manager echoes it
    back in the success response so the collector can verify the right batch
    was acknowledged before deleting the local queue.
    """
    queue_id: str
    name:     str   # system name  (mirrors header.device_name for quick routing)
    id:       str   # system id    (mirrors header.device_id)
    messages: List[Message]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "name":     self.name,
            "id":       self.id,
            "messages": [m.to_dict() for m in self.messages],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "QueuePayload":
        return QueuePayload(
            queue_id=d["queue_id"],
            name=d["name"],
            id=d["id"],
            messages=[Message.from_dict(m) for m in d.get("messages", [])],
        )


# ---------------------------------------------------------------------------
# Legacy model kept for any code that still references NASInfo
# (dashboard display only – not used in the ingest path)
# ---------------------------------------------------------------------------

@dataclass
class NASInfo:
    """Info about a NAS system (dashboard display only)."""
    nas_name:          str
    nas_id:            str
    last_update:       str   # ISO 8601
    total_usage_bytes: int
    folders:           List[Dict[str, Any]]  # [{path, usage_bytes}, ...]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
