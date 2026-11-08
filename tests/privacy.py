import os

def assert_private_file(path):
    if os.name == 'nt':
        import subprocess
        acl = subprocess.check_output(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "(Get-Acl -LiteralPath '" + str(path).replace("'", "''") + "').Sddl"], text=True)
        # Protected DACL with only the current user and SYSTEM.
        from sidepulse.windows_support import user_sid
        assert user_sid() in acl, acl
        parent_acl = subprocess.check_output(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "(Get-Acl -LiteralPath '" + str(path.parent).replace("'", "''") + "').Sddl"], text=True)
        assert 'D:P' in parent_acl, parent_acl
        import re
        trustees = re.findall(r'\([^)]*;;;([^;)]+)\)', acl)
        assert set(trustees) <= {user_sid(), 'SY'} and trustees
    else:
        assert path.stat().st_mode & 0o777 == 0o600
