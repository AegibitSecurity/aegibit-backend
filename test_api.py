import requests

# Test /deals endpoint
try:
    r = requests.get('http://localhost:8000/deals', headers={
        'X-Org-Id': '5aa6f273-8f1c-40b7-936b-d3586975df4d',
        'X-Role': 'SALES'
    })
    print(f"Status: {r.status_code}")
    print(f"Response: {r.text[:2000]}")
except Exception as e:
    print(f"Error: {e}")

# Test /dashboard/summary
try:
    r = requests.get('http://localhost:8000/dashboard/summary', headers={
        'X-Org-Id': '5aa6f273-8f1c-40b7-936b-d3586975df4d',
        'X-Role': 'SALES'
    })
    print(f"\nDashboard Status: {r.status_code}")
    print(f"Dashboard Response: {r.text[:1000]}")
except Exception as e:
    print(f"Dashboard Error: {e}")

# Test /variants
try:
    r = requests.get('http://localhost:8000/variants', headers={
        'X-Org-Id': '5aa6f273-8f1c-40b7-936b-d3586975df4d',
        'X-Role': 'SALES'
    })
    print(f"\nVariants Status: {r.status_code}")
    print(f"Variants Response: {r.text[:500]}")
except Exception as e:
    print(f"Variants Error: {e}")
