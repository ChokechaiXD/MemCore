#!/usr/bin/env python3
"""Setup scratch copy of the live memcore store for testing."""
import shutil
import os
import subprocess
import sys

src = 'C:/Users/BlankScreen/.memcore/memory.db'
local_app = os.environ.get('LOCALAPPDATA', '/tmp')
dst = os.path.join(local_app, 'memcore_test.db')

if os.path.exists(dst):
    for suffix in ('', '-wal', '-shm'):
        try:
            os.unlink(dst + suffix)
        except OSError:
            pass

shutil.copy2(src, dst)
# Copy WAL/SHM if they exist
for suffix in ('-wal', '-shm'):
    try:
        shutil.copy2(src + suffix, dst + suffix)
    except OSError:
        pass

print(f'Scratch copy ready at {dst}')