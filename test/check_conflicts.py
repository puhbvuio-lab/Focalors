import sys
import inspect
sys.path.insert(0, ".")

from PyQt5.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from src.studio.registry import TOOLS
from src.studio.base import load_object

issues = []
for tool in TOOLS:
    try:
        cls = load_object(tool.entrypoint)
        instance = cls()
    except Exception as e:
        continue

    form_fields = {f.name for f in instance.fields}
    config_params = {p.key: p.default for p in instance.tool_config_params()}
    # Check overlap
    overlap = form_fields & set(config_params.keys())
    if overlap:
        print(f"*** CONFLICT in {tool.name}: form fields = config params: {overlap}")
        issues.append((tool.name, overlap))

    # Check: config params that are passed in run_task but NOT as config= dict
    try:
        src = inspect.getsource(cls.run_task)
    except Exception:
        continue
    for cp in config_params:
        # Check if cp appears in the "config =" dict comprehension
        # or in a k.startswith pattern
        # If it's NOT in the config dict filter, and NOT passed as a direct param,
        # then it's not reaching the scraper
        if cp not in src:
            print(f"  {tool.name}: config param '{cp}' not found in run_task source")

    try:
        instance.close()
    except Exception:
        pass
    del instance

if not issues:
    print("No form-field/config-param name conflicts found.")
else:
    print(f"\n{len(issues)} conflicts total.")
print("Done.")
