import requests

api_url = "http://online_store:9000"
email = "tiaanf@8ohm.co.za"
password = "!DNsEA#5kU"

# Authenticate
resp = requests.post(f"{api_url}/auth/user/emailpass", json={"email": email, "password": password})
token = resp.json().get("token")
print("Token:", token)

headers = {"Authorization": f"Bearer {token}"}

# Get Sales Channels
resp = requests.get(f"{api_url}/admin/sales-channels", headers=headers)
print("Sales Channels:", [c.get("name") for c in resp.json().get("sales_channels", [])])

# Get Shipping Profiles
resp = requests.get(f"{api_url}/admin/shipping-profiles", headers=headers)
print("Shipping Profiles:", [p.get("name") for p in resp.json().get("shipping_profiles", [])])

# Get Stock Locations
resp = requests.get(f"{api_url}/admin/stock-locations", headers=headers)
print("Stock Locations:", [l.get("name") for l in resp.json().get("stock_locations", [])])
