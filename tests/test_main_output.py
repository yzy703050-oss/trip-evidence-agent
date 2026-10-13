import io
import json
import pytest
from rich.console import Console
from cli import TripEvidenceCLI
from agents.execution_harness import ExecutionHarness
from context.memory_manager import MemoryManager
from test_main_harness import Main, Info
