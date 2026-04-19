import sqlite3

conn = sqlite3.connect('aegibit_flow.db')
cursor = conn.cursor()

# Check tables
cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
tables = cursor.fetchall()
print('Tables:', tables)

# Check organizations
cursor.execute('SELECT * FROM organizations')
orgs = cursor.fetchall()
print('Organizations:', orgs)

# Check deals with dates
cursor.execute('SELECT id, customer_name, status, created_at FROM deals ORDER BY created_at DESC LIMIT 10')
deals = cursor.fetchall()
print('Deals (last 10):')
for d in deals:
    print(f'  {d}')

# Count total deals
cursor.execute('SELECT COUNT(*) FROM deals')
print('Total deals:', cursor.fetchone()[0])

conn.close()
