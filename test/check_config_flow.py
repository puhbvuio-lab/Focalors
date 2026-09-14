"""Diagnose config parameter flow issues across all tool windows."""
import sys
sys.path.insert(0, ".")

import inspect
from src.studio.registry import TOOLS
from src.studio.base import load_object

for tool in TOOLS:
    print(f"\n{'='*60}")
    print(f"Tool: {tool.name} ({tool.tool_id})")
    try:
        cls = load_object(tool.entrypoint)
    except Exception as e:
        print(f"  SKIP: cannot load - {e}")
        continue

    # Get form field names
    try:
        instance = cls()
    except Exception as e:
        print(f"  SKIP: cannot instantiate - {e}")
        continue

    form_fields = [f.name for f in instance.fields]
    config_params = [p.key for p in instance.tool_config_params()]

    print(f"  Form fields ({len(form_fields)}): {form_fields}")
    print(f"  Config params ({len(config_params)}): {config_params}")

    # Check for overlapping keys (form field name == config param key)
    overlaps = set(form_fields) & set(config_params)
    if overlaps:
        print(f"  *** CONFLICT: form fields overwritten by config: {overlaps}")

    # Check run_task's config filter
    source = inspect.getsource(cls.run_task)
    # Extract config filter keys from the dict comprehension
    import re
    # Look for: config = {k: v for k, v in values.items() if k in (...)}
    filter_match = re.search(r'if k in \(([^)]+)\)', source)
    filter_match2 = re.search(r'if k\.startswith\("([^"]+)"\) or k in \(([^)]+)\)', source)
    if filter_match2:
        prefix = filter_match2.group(1)
        listed = [s.strip().strip('"') for s in filter_match2.group(2).split(",")]
        config_filter_keys = [f"prefix:{prefix}"] + listed
    elif filter_match:
        config_filter_keys = [s.strip().strip('"') for s in filter_match.group(1).split(",")]
    else:
        config_filter_keys = ["(none - config not passed)"]

    print(f"  Config filter keys: {config_filter_keys}")

    # Check if all config params appear in the filter or are passed through
    passed_through = set(config_params) - set(form_fields)
    for cp in config_params:
        if cp in ("youtube_search_batch_size", "youtube_video_batch_size", "youtube_api_page_size", "comment_top_limit"):
            # These use startswith prefix
            continue
        # Simplified check: is the key in the filter list?
        found = False
        for fk in config_filter_keys:
            if cp == fk or cp.startswith(fk.replace("prefix:", "")):
                found = True
                break
        if not found and cp not in overlaps:
            print(f"  *** ORPHANED: config param '{cp}' not in run_task filter")

    instance.close() if hasattr(instance, 'close') else None
    del instance

print("\nDone.")
