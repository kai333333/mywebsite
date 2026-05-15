from flask import Flask, render_template, request, jsonify, send_file
import json
import requests
import os
import base64
from io import BytesIO
from PIL import Image

app = Flask(__name__)

# ===================== 配置 =====================
SGLANG_API_URL = "http://172.16.6.4:30000"  # 本地SGLang
MODEL_POINTS_COST = {
    "basic": 3,
    "advanced": 5,
    "pro": 8,
    "image_gen": 5,
    "image_gen_hd": 8,
    "image_gen_uhd": 12,
    "img2img": 6,
    "enhance_upscale": 3,
    "enhance_beauty": 2,
    "enhance_removebg": 4,
    "enhance_style": 3
}
PLANS = {"10": 100, "20": 220, "50": 600}
USER_DB = "users.json"
# 缓存生成图（内存，重启清空；正式可改用static/）
IMG_CACHE = {}
# ================================================

def load_users():
    if not os.path.exists(USER_DB):
        with open(USER_DB, "w") as f:
            json.dump({}, f)
    with open(USER_DB, "r") as f:
        return json.load(f)

def save_users(users):
    with open(USER_DB, "w") as f:
        json.dump(users, f, indent=2)

# 首页
@app.route('/')
def index():
    return render_template('index.html')

# 登录/注册
@app.route('/api/login', methods=['POST'])
def login():
    email = request.json.get('email')
    users = load_users()
    if email not in users:
        users[email] = {"points": 10, "history": []}
        save_users(users)
    return jsonify({"success": True, "user": {"email": email, **users[email]}})

# 充值
@app.route('/api/recharge', methods=['POST'])
def recharge():
    email = request.json.get('email')
    amount = request.json.get('amount')
    if amount not in PLANS:
        return jsonify({"success": False, "msg": "Invalid plan"})
    users = load_users()
    add = PLANS[amount]
    users[email]["points"] += add
    users[email]["history"].append(f"Recharge ${amount} → +{add} points")
    save_users(users)
    return jsonify({"success": True, "points": users[email]["points"]})

# 文本聊天
@app.route('/api/ai', methods=['POST'])
def ai_generate():
    data = request.json
    email, prompt, model = data.get('email'), data.get('prompt'), data.get('model','basic')
    users = load_users()
    cost = MODEL_POINTS_COST[model]
    if users[email]["points"] < cost:
        return jsonify({"success": False, "msg": "Insufficient points"})
    try:
        res = requests.post(f"{SGLANG_API_URL}/generate", json={
            "text": prompt, "max_new_tokens": 512, "temperature": 0.7
        }, timeout=30)
        if res.status_code != 200:
            return jsonify({"success": False, "msg": "AI error"})
        users[email]["points"] -= cost
        users[email]["history"].append(f"Chat {model} → -{cost}pts")
        save_users(users)
        return jsonify({"success": True, "result": res.json()["text"], "remaining": users[email]["points"]})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

# 文生图
@app.route('/api/image_gen', methods=['POST'])
def image_gen():
    data = request.json
    email, prompt, size = data.get('email'), data.get('prompt'), data.get('size','512')
    users = load_users()
    cost_key = {"512":"image_gen","768":"image_gen_hd","1024":"image_gen_uhd"}[size]
    cost = MODEL_POINTS_COST[cost_key]
    if users[email]["points"] < cost:
        return jsonify({"success": False, "msg": "Insufficient points"})
    try:
        res = requests.post(f"{SGLANG_API_URL}/v1/images/generations", json={
            "prompt": prompt, "size": f"{size}x{size}", "n": 1, "response_format": "b64_json"
        }, timeout=60)
        if res.status_code != 200:
            return jsonify({"success": False, "msg": "Image gen error"})
        b64 = res.json()["data"][0]["b64_json"]
        img_id = f"img_{os.urandom(8).hex()}"
        IMG_CACHE[img_id] = b64
        users[email]["points"] -= cost
        users[email]["history"].append(f"Image {size} → -{cost}pts | {img_id}")
        save_users(users)
        return jsonify({"success": True, "img_id": img_id, "remaining": users[email]["points"]})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

# 图生图
@app.route('/api/img2img', methods=['POST'])
def img2img():
    data = request.json
    email, prompt, img_b64 = data.get('email'), data.get('prompt'), data.get('image')
    users = load_users()
    cost = MODEL_POINTS_COST["img2img"]
    if users[email]["points"] < cost:
        return jsonify({"success": False, "msg": "Insufficient points"})
    try:
        res = requests.post(f"{SGLANG_API_URL}/v1/images/generations", json={
            "prompt": prompt, "image": img_b64, "size": "512x512", "n": 1, "response_format": "b64_json"
        }, timeout=60)
        if res.status_code != 200:
            return jsonify({"success": False, "msg": "Img2img error"})
        b64 = res.json()["data"][0]["b64_json"]
        img_id = f"img_{os.urandom(8).hex()}"
        IMG_CACHE[img_id] = b64
        users[email]["points"] -= cost
        users[email]["history"].append(f"Img2img → -{cost}pts | {img_id}")
        save_users(users)
        return jsonify({"success": True, "img_id": img_id, "remaining": users[email]["points"]})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

# 图片美化（调用SGLang/本地简易处理，这里先对接SGLang图生图做风格化）
@app.route('/api/enhance', methods=['POST'])
def enhance():
    data = request.json
    email, img_b64, mode = data.get('email'), data.get('image'), data.get('mode')
    users = load_users()
    cost = MODEL_POINTS_COST[f"enhance_{mode}"]
    if users[email]["points"] < cost:
        return jsonify({"success": False, "msg": "Insufficient points"})
    prompt_map = {
        "upscale": "4x upscale, sharp, detailed",
        "beauty": "beauty, smooth skin, soft light",
        "removebg": "remove background, transparent",
        "style": "anime style, masterpiece"
    }
    try:
        res = requests.post(f"{SGLANG_API_URL}/v1/images/generations", json={
            "prompt": prompt_map[mode], "image": img_b64, "size": "512x512", "n": 1, "response_format": "b64_json"
        }, timeout=60)
        if res.status_code != 200:
            return jsonify({"success": False, "msg": "Enhance error"})
        b64 = res.json()["data"][0]["b64_json"]
        img_id = f"img_{os.urandom(8).hex()}"
        IMG_CACHE[img_id] = b64
        users[email]["points"] -= cost
        users[email]["history"].append(f"Enhance {mode} → -{cost}pts | {img_id}")
        save_users(users)
        return jsonify({"success": True, "img_id": img_id, "remaining": users[email]["points"]})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

# 获取图片（用于预览/下载）
@app.route('/api/get_img/<img_id>')
def get_img(img_id):
    if img_id not in IMG_CACHE:
        return "Not found", 404
    b64 = IMG_CACHE[img_id]
    return send_file(BytesIO(base64.b64decode(b64)), mimetype="image/png")

# 用户信息
@app.route('/api/user', methods=['POST'])
def get_user():
    email = request.json.get('email')
    users = load_users()
    return jsonify({"user": {"email": email, **users[email]}})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=50000, debug=True)
