from flask import Flask, render_template, request
from common.params import Params
from urllib.parse import parse_qs, unquote
import requests
import json
import sys
import os

app = Flask(__name__)
params = Params()

def main():
    app.run(host='0.0.0.0', port=8000)

@app.route('/')
def hello():
    return render_template('index.html')

@app.route('/parse', methods=['POST'])
def parse():
    if request.method == 'POST':
        address = request.form['address'] + " " + request.form['address2'] + " " + request.form['city'] + " " + request.form['state'] + " " + request.form['zip']
        url = request.form['url']
        get_coords(url, address)
        return address

def get_coords(url, address):
    # parse url by apple maps
    if url.split("https://")[-1].split("/")[0] == "maps.apple.com":
        coords = url.split("ll=")[-1].split("&")[0].split(",")
        dest = {
          "latitude": float(coords[0]),
          "longitude": float(coords[1])
        }
        setParam(dest)
    # parse url by google maps
    elif url.split("https://")[-1].split("/")[0] == "www.google.com":
        coords = url.split("/@")[-1].split("/")[0].split(",")
        dest = {
        "latitude": float(coords[0]),
        "longitude": float(coords[1])
        }
        setParam(dest)
    # parse url by address
    else:
        api_key = os.getenv('MAPBOX_TOKEN')
        g = "https://api.mapbox.com/geocoding/v5/mapbox.places/" + unquote(address) + ".json?access_token=" + api_key + "&limit=1"
        coords = requests.get(g).json()["features"][0]["geometry"]["coordinates"]
        coords_str = str(coords)
        dest = {
            "latitude": float(coords_str.split(",")[-1].split("]")[0]),
            "longitude": float(coords_str.split("[")[-1].split(",")[0]),
        }
        print(dest)
        setParam(dest)
    
    
def setParam(dest):
    params.put("NavDestination", json.dumps(dest))
    params.remove("NavDestinationWaypoints")
    params.put("ApiCache_NavDestinations", json.dumps(dest).rstrip("\n\r"))

@app.route('/clear')
def clearNav():
    params.put("NavDestination", "")
    return None

if __name__ == '__main__':
    main()