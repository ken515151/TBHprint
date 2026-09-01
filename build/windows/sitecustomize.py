"""Shipped into the bundled runtime's Lib\site-packages by build.ps1.

The install lives in a per-user directory that Inno Setup's uninstaller
only knows how to remove file-by-file, so nothing may ever write into it
at runtime. Python's import system would otherwise drop __pycache__
directories beside every module the first time it runs - `-B` on the
command line covers our own launches, this covers everything else (a
support engineer running python.exe by hand, a future launcher without
the flag). Bytecode caching buys nothing here: the agent starts once at
logon and runs all day.
"""
import sys

sys.dont_write_bytecode = True
