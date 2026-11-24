"""Code-owned, explicit industry registration. No dynamic untrusted imports."""
from .logistics import PACK as LOGISTICS

EMPTY = {"modules": {}, "business_tools": False, "tool_groups": {}, "route_groups": {}, "state_groups": {}}
PACKS = {"logistics": LOGISTICS, "general": EMPTY}

def get_pack(industry):
    return PACKS.get(industry, EMPTY)
