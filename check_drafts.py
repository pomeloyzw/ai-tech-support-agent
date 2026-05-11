import sqlite3
import json

db_path = r'D:\projects\ai-tech-support-agent\support_agent.db'

query = """
SELECT c.id, c.status, c.issue_type, c.severity,
       json_extract(d.evidence_json, '$.confidence'),
       json_extract(d.evidence_json, '$.suggested_action'),
       d.evidence_json
FROM cases c
JOIN drafts d ON c.id = d.case_id
"""

try:
    with sqlite3.connect(db_path) as db:
        res = db.execute(query).fetchall()
        print(f"\n--- FOUND {len(res)} MATCH(ES) ---\n")
        for r in res:
            print(f"Case ID: {r[0]}")
            print(f"Status: {r[1]} | Type: {r[2]} | Severity: {r[3]}")
            print(f"Confidence: {r[4]} | Action: {r[5]}")
            print("\nEvidence JSON:")
            try:
                print(json.dumps(json.loads(r[6]), indent=2))
            except:
                print(r[6])
            print("-" * 50)
except Exception as e:
    print(f"Error querying database: {e}")
