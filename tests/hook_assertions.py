from sidepulse.hook_commands import observer_script

def command_text(command):
    script = observer_script(command)
    return script.replace("' '", " ") if script != command else command

def fail_open(command):
    script = observer_script(command)
    return script.rstrip().endswith('exit 0') if script != command else command.rstrip().endswith('; true')
