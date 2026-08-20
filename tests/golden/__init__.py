"""Golden regression harness for M10a.

The whole milestone rests on one promise: crypto output does not change. These
modules capture what the deterministic pipeline and every user-facing surface
produce **today**, byte for byte, so any drift introduced while adding the market
dimension fails a test instead of quietly contaminating a live measurement window.
"""
