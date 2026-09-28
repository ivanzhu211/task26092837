# 活动重复场次排期模块

纯标准库（`sqlite3`、`datetime`、`zoneinfo`）实现，状态持久化到单个 SQLite 文件。

## 使用

```python
from datetime import datetime
from scheduling import Scheduler, Rule

sched = Scheduler("sessions.db")

series_id = sched.create_series(Rule(
    timezone="America/New_York",
    first_local_start=datetime(2024, 3, 3, 2, 30),  # 本地墙钟 naive
    duration_minutes=60,                              # 按真实流逝计算
    period="weekly",                                  # weekly|biweekly|monthly
    count=4,                                          # 1..52
))

# 改期 / 取消单场（None 表示取消）
sched.override(occurrence_id, datetime(2024, 3, 10, 4, 0))
sched.override(occurrence_id, None)

# 从第 k 场起按新规则重排
sched.edit_from(series_id, k, new_rule)

sched.register(occurrence_id, "a@example.com")
sched.can_check_in(occurrence_id, datetime.now(timezone.utc))
for occ in sched.list_occurrences(series_id):
    print(occ.id, occ.seq, occ.start_utc, occ.end_utc, occ.status)
```

## 时区语义

- 时区以 IANA 名保存在 `series.timezone`，不退化为固定偏移。
- 本地时间落在春季缺口：按缺口长度顺延（如 `02:30` → `03:30`）。
- 本地时间落在秋季重叠：取较早的时刻（`fold=0`）。
- 结束时间与签到判定只看绝对时刻：时长按真实流逝加在 UTC 时刻上。

## 测试

```
python3 -m pytest tests -q
```
