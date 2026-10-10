#!/bin/sh
# 压测状态速览。
# 用法：sh tools/stress_status.sh
echo "=== 进程 ==="
ps -eo pid,etime,args | grep "[s]tress_core\|[s]tress_gui" | awk '{printf "  pid=%-7s 已跑 %s  %s\n", $1, $2, $3}'

echo
echo "=== 核心层 ==="
tail -3 /tmp/opencode/stress/core.log 2>/dev/null | sed 's/^/  /'
echo "  bug 文件: $(wc -l < /tmp/ycpy-stress.jsonl 2>/dev/null || echo 0) 条"

echo
echo "=== GUI 层 ==="
tail -3 /tmp/opencode/stress/gui.log 2>/dev/null | sed 's/^/  /'
echo "  bug 文件: $(wc -l < /tmp/ycpy-stress-gui.jsonl 2>/dev/null || echo 0) 条"

echo
if [ -s /tmp/ycpy-stress.jsonl ]; then
  echo "=== 核心层 bug 分类 ==="
  python3 -c "
import json,collections
c=collections.Counter()
for l in open('/tmp/ycpy-stress.jsonl'):
    try: c[json.loads(l)['kind']]+=1
    except Exception: pass
for k,v in c.most_common(): print('  %-28s %d' % (k,v))
"
fi
if [ -s /tmp/ycpy-stress-gui.jsonl ]; then
  echo "=== GUI 层 bug 分类 ==="
  python3 -c "
import json,collections
c=collections.Counter()
for l in open('/tmp/ycpy-stress-gui.jsonl'):
    try: c[json.loads(l)['kind']]+=1
    except Exception: pass
for k,v in c.most_common(): print('  %-28s %d' % (k,v))
"
fi