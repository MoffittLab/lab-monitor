#!/usr/bin/env python3
"""
Quick test: measure a Hyper Backup vault path without recursing into it.
Run on the NAS:
  python3 test_vault_size.py
"""
import os
import shutil

VAULT_PATH = "/volume1/Triton1_V1_Backup"

def fmt(b):
    for unit in ['B','KB','MB','GB','TB']:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"

print(f"Testing: {VAULT_PATH}")
print()

# Method 1: os.path.getsize() — size of the vault entry itself
try:
    size = os.path.getsize(VAULT_PATH)
    print(f"os.path.getsize():      {fmt(size)}")
except Exception as e:
    print(f"os.path.getsize():      FAILED — {e}")

# Method 2: shutil.disk_usage() — total/used/free of the filesystem at that path
try:
    usage = shutil.disk_usage(VAULT_PATH)
    print(f"shutil.disk_usage():")
    print(f"  total:  {fmt(usage.total)}")
    print(f"  used:   {fmt(usage.used)}")
    print(f"  free:   {fmt(usage.free)}")
except Exception as e:
    print(f"shutil.disk_usage():    FAILED — {e}")

# Method 3: os.stat() — low-level blocks allocated on disk
try:
    st = os.stat(VAULT_PATH)
    blocks_bytes = st.st_blocks * 512
    print(f"os.stat() st_blocks:   {fmt(blocks_bytes)}  (512-byte blocks × {st.st_blocks})")
except Exception as e:
    print(f"os.stat():              FAILED — {e}")
