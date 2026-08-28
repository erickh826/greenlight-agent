"""Agent factories.

Each module exposes a build_* function rather than a module-level Agent. An
Agent holds a toolset, a toolset holds a subprocess, and a subprocess created at
import time is one nothing owns and nothing closes.
"""
