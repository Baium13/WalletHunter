"""User-selected limits for new independent AI entries, not copy/rescue.

The allocation is a ceiling before lot rounding, never a reason to round up
to the exchange minimum. Existing positions and persisted history are unchanged.
"""
ENTRY_MARGIN_FRACTION = .10
MAX_ENTRY_LEVERAGE = 40
ENTRY_POLICY_VERSION = 2
