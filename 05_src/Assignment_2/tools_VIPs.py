from langchain.tools import tool
import json
import requests
import pandas as pd

VIP_Customers = pd.read_csv('../05_src/Assignment_2_Data/vip_customers.csv')



@tool
def get_VIPs(n:int=1):
    """
    Returns number of VIPs that emailed the inbox.
    """
    url = VIP_Customers
    params = {
        "count": n
    }
    response = requests.get(url, params=params)
    resp_dict = json.loads(response.text)
    VIP_list = resp_dict.get("data", [])
    VIP = "\n".join([f"{i+1}. {fact}\n" for i, fact in enumerate(VIP_list)])
    return VIP