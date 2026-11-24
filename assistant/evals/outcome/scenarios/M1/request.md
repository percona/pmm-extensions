Every backup of the PostgreSQL Backups app reports failure on our PMM Client hosts. Here is the end of the log of one of them:

```text
  File "/usr/lib64/python3.9/pathlib.py", line 1367, in is_dir
    return S_ISDIR(self.stat().st_mode)
  File "/usr/lib64/python3.9/pathlib.py", line 1160, in stat
    return self._accessor.stat(self)
PermissionError: [Errno 13] Permission denied: '/root/.local/percona/logs/backup'
```
