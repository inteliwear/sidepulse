"""Read the maintained Python UI without importing AppKit or touching settings."""
import ast
import json
from pathlib import Path
root = Path(__file__).resolve().parents[3]
status = ast.parse((root / 'src/sidepulse/status_bar.py').read_text())
models = ast.parse((root / 'src/sidepulse/models.py').read_text())
settings_window = next(node for node in status.body if isinstance(node, ast.FunctionDef) and node.name == 'build_settings_window')
calls = sorted((node for node in ast.walk(settings_window) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)), key=lambda node: node.lineno)
constants={node.targets[0].id: node.value.value for node in settings_window.body if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and isinstance(node.value,ast.Constant)}
def literal(node):
    try:
        return constants.get(node.id) if isinstance(node,ast.Name) else ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
tabs = [[literal(node.args[1]), literal(node.args[2])] for node in calls if node.func.id == 'add_settings_tab']
sections = {}
for name, variable in [('agents','agents_tab'), ('advanced','behavior_tab'), ('diagnostics','diagnostics_tab')]:
    sections[name] = [literal(node.args[1]) for node in calls if node.func.id == 'add_label' and isinstance(node.args[0], ast.Name) and node.args[0].id == variable and isinstance(node.args[1], ast.Constant) and literal(node.args[2]) == 24]
hooks = [literal(node.args[0]) for node in calls if node.func.id == 'add_hook_row']
columns = [literal(node.args[1]) for node in calls if node.func.id == 'add_label' and isinstance(node.args[0], ast.Name) and node.args[0].id == 'agent_animations_tab' and literal(node.args[3]) == 287]
enum = next(node for node in models.body if isinstance(node, ast.ClassDef) and node.name == 'AgentMode')
values = {node.targets[0].id: literal(node.value) for node in enum.body if isinstance(node, ast.Assign)}
labels = next(node.value for node in models.body if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', '') == 'MODE_LABELS')
mode_labels = {values[key.attr]: literal(value) for key, value in zip(labels.keys, labels.values)}
rows = [[value, mode_labels[value]] for value in values.values() if value not in ('tool_running','long_task_progress')]
for node in status.body:
    if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Subscript) and getattr(node.targets[0].value,'id','') == 'ANIMATION_STATE_LABELS':
        key=node.targets[0].slice
        if isinstance(key,ast.Attribute) and isinstance(key.value,ast.Attribute) and key.value.attr in values:
            for row in rows:
                if row[0] == values[key.value.attr]: row[1] = literal(node.value)
rows += [['lid_open','Lid Open'], ['lid_closed','Lid Closed']]
history = next(literal(node.value) for node in ast.walk(status) if isinstance(node, ast.Assign) and any(getattr(target,'id','') == 'rows' for target in node.targets) and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 6)
durations={node.targets[0].id:literal(node.value) for node in status.body if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and node.targets[0].id in ('AGENT_ANIMATION_DEVICE_PREVIEW_SECONDS','AGENT_ANIMATION_EDITOR_DEVICE_PREVIEW_SECONDS')}
print(json.dumps(dict(durations=durations, tabs=tabs, sections=sections, hooks=hooks, columns=columns, animations=rows, history=[row[0] for row in history])))
