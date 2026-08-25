"""Kill the abandoned idle connection blocking the ALTER, then re-check."""

import frappe

rows = frappe.db.sql("SHOW FULL PROCESSLIST", as_dict=True)
me = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]

targets = [
    r for r in rows
    if r.get("Command") == "Sleep"
    and int(r.get("Time") or 0) > 300
    and int(r.get("Id")) != int(me)
]

print(f"my connection = {me}")
print(f"idle >300s connections to kill: {[r.get('Id') for r in targets]}")

for r in targets:
    cid = r.get("Id")
    try:
        frappe.db.sql(f"KILL {int(cid)}")
        print(f"  killed {cid} (idle {r.get('Time')}s)")
    except Exception as exc:
        print(f"  could NOT kill {cid}: {exc}")

print("\n=== processlist after ===")
for r in frappe.db.sql("SHOW FULL PROCESSLIST", as_dict=True):
    info = (r.get("Info") or "")[:60].replace("\n", " ")
    print(f"  id={r.get('Id')} cmd={r.get('Command')} time={r.get('Time')}s "
          f"state={str(r.get('State'))[:36]!r} :: {info}")
