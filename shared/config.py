"""
Shared configuration utilities
"""
import json
import os
from typing import Dict, Any


def load_config(config_path: str) -> Dict[str, Any]:
    """Load JSON config file"""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        return json.load(f)


def save_config(config: Dict[str, Any], config_path: str) -> None:
    """Save config to JSON file"""
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)


def get_config_with_env_overrides(config_path: str, env_prefix: str = "LAB_MONITOR_") -> Dict[str, Any]:
    """
    Load config from file and override with environment variables.
    
    Env var format: LAB_MONITOR_SECTION_KEY (e.g., LAB_MONITOR_MANAGER_URL)
    """
    config = load_config(config_path)
    
    # Check for env overrides
    for key, value in os.environ.items():
        if key.startswith(env_prefix):
            config_key = key[len(env_prefix):].lower()
            # Handle nested keys (e.g., SECTION_KEY)
            config[config_key] = value
    
    return config
