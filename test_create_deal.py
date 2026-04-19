import requests

headers = {
    'X-Org-Id': '5aa6f273-8f1c-40b7-936b-d3586975df4d',
    'X-Role': 'SALES',
    'Content-Type': 'application/json'
}

deal_data = {
    "customer_name": "Ab Ghosh",
    "mobile": "1234567890",
    "model": "Punch",
    "variant": "punch2.0 accomplished+s cngamt",
    "registration_type": "INDIVIDUAL",
    "discount": 5200,
    
    # Customer fields
    "phone": None,
    "father_name": "ABC",
    "address": "kalyani",
    "aadhaar": "123456789",
    "pan": "dajnm675g",
    "voter_id": "154565",
    "rse_name": "Abir",
    "sm_name": "bM",
    
    # Deal/CRM fields
    "delivery_date": "2026-04-15",
    "crm_date": "2026-04-14",
    "crm_invoice_no": "001",
    "crm_esp": 1,
    
    # Vehicle fields
    "colour": "Black",
    "chassis_no": "adsfwsew2e232ew",
    "engine_no": "swscscc32eqxa",
    
    # Finance fields
    "sale_type": "FINANCE",
    "financer_name": "awfwea",
    "financer_branch": "kal",
    "inhouse_finance": "YES",
    
    # RTO fields
    "rto_code": "WB-01",
    "rto_name": "Kolkata North (Kolkata)",
    "rto_district": "Kolkata",
    "branch": "kalyani",
}

try:
    r = requests.post('http://localhost:8000/create-deal', headers=headers, json=deal_data)
    print(f'Status: {r.status_code}')
    print(f'Response: {r.text[:2000]}')
except Exception as e:
    print(f'Error: {e}')
