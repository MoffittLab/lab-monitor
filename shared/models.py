"""
Shared data models for lab-monitor
"""
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Any
import json


@dataclass
class FolderUsage:
    """Single folder usage snapshot"""
    path: str
    usage_bytes: int
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class UsageReport:
    """Usage report from a collector"""
    nas_name: str
    nas_id: str
    timestamp: str  # ISO 8601
    folders: List[FolderUsage]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "nas_name": self.nas_name,
            "nas_id": self.nas_id,
            "timestamp": self.timestamp,
            "folders": [f.to_dict() for f in self.folders]
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict())
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "UsageReport":
        folders = [FolderUsage(**f) for f in data.get("folders", [])]
        return UsageReport(
            nas_name=data["nas_name"],
            nas_id=data.get("nas_id", data["nas_name"]),
            timestamp=data["timestamp"],
            folders=folders
        )


@dataclass
class NASInfo:
    """Info about a NAS system"""
    nas_name: str
    nas_id: str
    last_update: str  # ISO 8601
    total_usage_bytes: int
    folders: List[Dict[str, Any]]  # [{path, usage_bytes}, ...]
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
