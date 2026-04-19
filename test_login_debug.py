"""Debug login issue"""
import sqlite3
import os

# Use absolute path
db_path = r'c:\Users\rmond\OneDrive\Desktop\Aegibit SaaS\backend\aegibit_flow.db'
print(f"Database path: {db_path}")
print(f"Database exists: {os.path.exists(db_path)}")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Check all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print(f"\nTables in database: {[t[0] for t in tables]}")

# Check users table
if ('users',) in tables:
    cursor.execute("SELECT id, email, role, is_active, organization_id FROM users WHERE email = 'pradip.ss19@gmail.com'")
    user = cursor.fetchone()

    if user:
        print(f"\nUser found: {user}")
    else:
        print("\nUser not found!")
        cursor.execute("SELECT email, role, is_active FROM users")
        all_users = cursor.fetchall()
        if all_users:
            print(f"\nAll users in database ({len(all_users)} total):")
            for u in all_users:
                print(f"  - Email: {u[0]}, Role: {u[1]}, Active: {u[2]}")
        else:
            print("\nNo users in database - need to seed!")
else:
    print("\n'users' table not found!")

conn.close()
