#!/usr/bin/env bash

# ---
# title: "MongoDB Replication Lag Check"
# description: "This script checks replication lag between primary and secondaries using replica set status."
# allow_extra_args: false
# sudo: optional
# diagnostic_categories:
#  - REPLICA_SET_REPLICATION
# service_type: mongodb
# alerts:
#   - MongoDBReplicationLag
# ---

# Usage: ./mongodb_repl_lag_check.sh

set -euo pipefail

MONGOSH="mongosh --quiet"
command -v mongosh &> /dev/null || MONGOSH="mongo --quiet"

echo "********* Replica set member lag *********"
echo ""
$MONGOSH --eval "
var status = rs.status();
var primary = null;
status.members.forEach(function(m) {
    if (m.stateStr === 'PRIMARY') primary = m;
});
if (primary) {
    var primaryOptimeDate = primary.optimeDate;
    var primaryOptimeStr = primaryOptimeDate ? tojson(primaryOptimeDate) : 'N/A';
    print('Primary: ' + primary.name + '  optime: ' + primaryOptimeStr);
    status.members.forEach(function(m) {
        if (m.stateStr !== 'PRIMARY') {
            var memberOptimeStr = m.optimeDate ? tojson(m.optimeDate) : 'N/A';
            var lagStr = 'N/A';
            if (primaryOptimeDate && m.optimeDate) {
                var lagMs = primaryOptimeDate - m.optimeDate;
                if (!isNaN(lagMs)) {
                    lagStr = (lagMs / 1000) + 's';
                }
            }
            print('  ' + m.name + '  state: ' + m.stateStr + '  lag: ' + lagStr + '  optime: ' + memberOptimeStr);
        }
    });
} else {
    print('No PRIMARY found in replica set.');
    status.members.forEach(function(m) {
        print('  ' + m.name + '  state: ' + m.stateStr + '  health: ' + m.health);
    });
}
" 2> /dev/null || echo "Cannot retrieve replica set status."

echo ""
echo "********* Replication info (oplog) *********"
echo ""
$MONGOSH --eval "printjson(db.getReplicationInfo())" 2> /dev/null || true

echo ""
echo "********* Current operations on primary *********"
echo ""
$MONGOSH --eval "
db.currentOp({'secs_running': {\$gt: 10}}).inprog.forEach(function(op) {
    print('OpID: ' + op.opid + '  secs: ' + op.secs_running + '  NS: ' + op.ns + '  plan: ' + (op.planSummary || 'N/A'));
});
" 2> /dev/null || true

echo ""
echo "********* Disk IO on secondary *********"
echo ""
if command -v iostat &> /dev/null; then
    iostat -xz 1 3 | tail -15
else
    echo "iostat not available."
fi
