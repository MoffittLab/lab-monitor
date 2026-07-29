"""
Shared utility functions
"""
import logging
import os
from typing import Optional


def setup_logging(log_file: Optional[str] = None, log_level: str = "INFO") -> logging.Logger:
    """Set up logging for a service"""
    logger = logging.getLogger(__name__)
    logger.setLevel(getattr(logging, log_level))
    
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler (if specified)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def format_bytes(bytes_value: int) -> str:
    """Convert bytes to human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024
    return f"{bytes_value:.2f} PB"


def get_percentage(used: int, total: int) -> float:
    """Calculate percentage used"""
    if total == 0:
        return 0.0
    return (used / total) * 100
