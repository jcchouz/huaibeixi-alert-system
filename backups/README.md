# 远程数据库覆盖前备份

- 备份时间：2026-10-29 16:47:19 +08:00
- 备份来源：`82.156.158.31:5432/huaibeixi_db_test`
- 备份文件：`huaibeixi_db_test_remote_before_schema_overwrite_20261029.dump`
- 文件格式：PostgreSQL 自定义格式（`pg_dump --format=custom`）
- SHA-256：`5728B14EBB03C3CC98EBAB60D6530EF0001C2C07EDAB7744C3E5F5D54F1AA975`

备份包含以下表的结构、索引、约束、触发器和数据：

| 表名 | 备份时行数 |
| --- | ---: |
| `t_alert_record` | 313 |
| `t_alert_threshold` | 208 |
| `t_stat_record` | 272534 |

恢复前请确认目标数据库，恢复操作会覆盖同名对象：

```powershell
pg_restore --clean --if-exists --no-owner --no-privileges `
  --host <数据库地址> --port 5432 --username <用户名> --dbname huaibeixi_db_test `
  .\backups\huaibeixi_db_test_remote_before_schema_overwrite_20261029.dump
```
