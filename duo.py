#!/usr/bin/env python3
"""Old name for AgentLens: `duo ...` keeps working and runs agentlens.py."""
import runpy
from pathlib import Path

if __name__=='__main__':runpy.run_path(str(Path(__file__).with_name('agentlens.py')),run_name='__main__')
