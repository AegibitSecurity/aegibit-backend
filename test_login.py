#!/usr/bin/env python3
"""
Test script to verify JWT authentication and user management endpoints.
"""
import requests

BASE = "http://localhost:8000"

# Test 1: Login as ADMIN
print("Test 1: Login as ADMIN")
resp = requests.post(f"{BASE}/auth/login", json={
    "email": "pradip.ss19@gmail.com",
    "password": "Pradip@1998"
})
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    data = resp.json()
    print(f"Token: {data['access_token'][:50]}...")
    print(f"User: {data['user']['email']} ({data['user']['role']})")
    token = data['access_token']
else:
    print(f"Error: {resp.text}")
    exit(1)

headers = {"Authorization": f"Bearer {token}"}

# Test 2: Get current user (me)
print("\nTest 2: Get current user")
resp = requests.get(f"{BASE}/auth/me", headers=headers)
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    print(f"Response: {resp.json()}")

# Test 3: List users (ADMIN only)
print("\nTest 3: List users")
resp = requests.get(f"{BASE}/users", headers=headers)
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    users = resp.json()
    print(f"Users found: {len(users)}")
    for u in users:
        print(f"  - {u['email']} ({u['role']}) - {'Active' if u['is_active'] else 'Inactive'}")

# Test 4: Create a new user (ADMIN only)
print("\nTest 4: Create SALES user")
# First get org ID
resp = requests.get(f"{BASE}/organizations", headers=headers)
orgs = resp.json()
org_id = orgs[0]['id'] if orgs else None

resp = requests.post(f"{BASE}/users", headers=headers, json={
    "email": "test.sales@example.com",
    "password": "TestPass123",
    "role": "SALES",
    "organization_id": org_id
})
print(f"Status: {resp.status_code}")
if resp.status_code == 201:
    print(f"Created: {resp.json()['email']}")
else:
    print(f"Error: {resp.text}")

# Test 5: Try login as new user
print("\nTest 5: Login as new SALES user")
resp = requests.post(f"{BASE}/auth/login", json={
    "email": "test.sales@example.com",
    "password": "TestPass123"
})
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    print("Sales user login successful!")
    sales_token = resp.json()['access_token']
    
    # Test 6: Try to access admin-only endpoint as SALES (should fail)
    print("\nTest 6: SALES user tries to list users (should fail)")
    resp = requests.get(f"{BASE}/users", headers={"Authorization": f"Bearer {sales_token}"})
    print(f"Status: {resp.status_code} (expected 403)")
    if resp.status_code == 403:
        print("✓ Correctly rejected non-admin access")
else:
    print(f"Error: {resp.text}")

# Test 7: Try to create ADMIN via API (should fail)
print("\nTest 7: Try to create ADMIN via API (should fail)")
resp = requests.post(f"{BASE}/users", headers=headers, json={
    "email": "hacker@example.com",
    "password": "Hacker123",
    "role": "ADMIN",
    "organization_id": org_id
})
print(f"Status: {resp.status_code} (expected 422 or 403)")
if resp.status_code in [422, 403]:
    print("✓ Correctly rejected ADMIN creation via API")

print("\n" + "="*50)
print("All tests completed!")
