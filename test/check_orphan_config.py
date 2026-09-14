"""Check each tool's config flow: param defined → passed → read by scraper."""
import sys
import re
import inspect
import importlib
sys.path.insert(0, ".")

from PyQt5.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from src.studio.registry import TOOLS
from src.studio.base import load_object

for tool in TOOLS:
    try:
        cls = load_object(tool.entrypoint)
        instance = cls()
    except Exception:
        continue

    params = instance.tool_config_params()
    if not params:
        continue

    param_keys = {p.key for p in params}

    # Get run_task source
    try:
        src = inspect.getsource(cls.run_task)
    except Exception:
        continue

    # Extract config filter keys
    filter_keys = set()
    m = re.search(r'k in \(([^)]+)\)', src)
    if m:
        for part in m.group(1).split(','):
            k = part.strip().strip('"\'')
            if k:
                filter_keys.add(k)
    # Check for startswith pattern
    m2 = re.search(r'k\.startswith\("([^"]+)"\)', src)
    prefix = m2.group(1) if m2 else None

    passed = set()
    for k in param_keys:
        if k in filter_keys:
            passed.add(k)
        elif prefix and k.startswith(prefix):
            passed.add(k)

    not_passed = param_keys - passed
    if not_passed:
        print(f"{tool.name} ({tool.tool_id}):")
        print(f"  NOT PASSED by run_task: {not_passed}")

    # Now check scraper reads
    imp_path = tool.implementation_path.replace('/', '.').replace('.py', '')
    try:
        scraper_mod = importlib.import_module(f'src.{imp_path}')
    except Exception:
        continue

    # Find the scraper function
    scraper_func = None
    src2 = inspect.getsource(cls.run_task)
    scraper_name = re.search(r'from src\.\S+ import (\w+)', src2)
    scraper_name2 = re.search(r'from src\.\S+\.\w+ import (\w+)', src2)
    func_name = (scraper_name.group(1) if scraper_name else
                 scraper_name2.group(1) if scraper_name2 else None)
    if func_name and hasattr(scraper_mod, func_name):
        scraper_func = getattr(scraper_mod, func_name)

    if scraper_func:
        scraper_src = inspect.getsource(scraper_func)
        for k in param_keys:
            if f'config.get("{k}"' not in scraper_src and f"config.get('{k}'" not in scraper_src:
                print(f"  ORPHANED: '{k}' not read by {func_name}()")

    instance.close() if hasattr(instance, 'close') else None
    del instance

print("Done.")
