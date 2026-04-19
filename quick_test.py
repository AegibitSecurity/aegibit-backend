"""Quick test to verify backend is responding"""
import requests

try:
    # Test health endpoint (if exists) or just check if server is up
    response = requests.get('http://localhost:8000/', timeout=5)
    print(f"Server responded: {response.status_code}")
    print(f"Response: {response.text[:200]}")
except requests.exceptions.ConnectionError:
    print("ERROR: Cannot connect to backend server on port 8000")
    print("The server is not running or not accessible")
except Exception as e:
    print(f"Error: {e}")
