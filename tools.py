# tools.py
import httpx
import json
import logging
from typing import Dict, Any , List
from config import GAODE_API_KEY, GAODE_GEO_URL, GAODE_WEATHER_URL

API_TIMEOUT = 10.0
async def get_weather(location: str) -> str:
    """调用高德API获取天气信息"""
    logging.info(f"【天气查询】开始获取 {location} 的天气")
    geo_params = {"key": GAODE_API_KEY, "address": location}
    
    async with httpx.AsyncClient() as client:
        geo_response = await client.get(GAODE_GEO_URL, params=geo_params,timeout=API_TIMEOUT)
        try:
            geo_data = geo_response.json()
        except json.JSONDecodeError:
            error_text = geo_response.text[:200] 
            logging.error(f"【地理编码】无法解析JSON，原始响应: {error_text}")
            return f"地理编码服务异常，无法解析响应"
            
        if geo_data["status"] != "1" or len(geo_data["geocodes"]) == 0:
            return f"无法找到城市: {location}"
        
        adcode = geo_data["geocodes"][0]["adcode"]
        
        weather_params = {"key": GAODE_API_KEY, "city": adcode, "extensions": "base"}
        weather_response = await client.get(GAODE_WEATHER_URL, params=weather_params,timeout=API_TIMEOUT)
        try:
           weather_data = weather_response.json()
        except json.JSONDecodeError:
            error_text = weather_response.text[:200]  # 截取前200字符，看实际返回了什么
            logging.error(f"【天气查询】无法解析JSON，原始响应: {error_text}")
            return f"天气查询服务异常，无法解析响应"
        if weather_data["status"] == "1":
            live_data = weather_data["lives"][0]
            result = f"{location}的天气: {live_data['weather']}, 温度: {live_data['temperature']}°C"
            logging.info(f"【天气工具】成功返回: {result}")
            return result
        else:
            err_info = weather_data.get("info", "未知原因")
            msg = f"获取天气失败: {err_info}"
            logging.warning(f"【天气查询】API返回失败: {msg}")
            return msg
        
async def search_places(keyword: str, city: str, limit: int = 15) -> str:
    """
    调用高德地图POI搜索API，查找城市内的地点
    :param keyword: 搜索关键词（如"黄鹤楼"、"火锅"、"酒店"）
    :param city: 城市名（如"武汉"）
    :param limit: 返回结果数量（默认10-15条）
    :return: 格式化后的地点信息字符串
    """
    url = "https://restapi.amap.com/v3/place/text"
    params = {
        "key": GAODE_API_KEY,
        "keywords": keyword,
        "city": city,
        "offset": limit,
        "page": 1,
        "extensions": "all",  # 获取详细信息
        "output": "json"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=10.0)
            data = response.json()

            if data["status"] != "1":
                return f"高德API错误: {data.get('info', '未知错误')}"

            pois = data.get("pois", [])
            if not pois:
                return f"在{city}未找到与'{keyword}'相关的地点。"

            results = []
            for i, poi in enumerate(pois[:limit], 1):
                name = poi.get("name", "未知")
                address = poi.get("address", "无地址")
                rating = poi.get("biz_ext", {}).get("rating", "暂无评分")
                tel = poi.get("tel", "无电话")
                
                # 构建简洁结果
                info = f"{i}. {name}"
                if address and address != "[]":
                    info += f"（{address}）"
                if rating != "[]":
                    info += f" ⭐{rating}"
                results.append(info)

            return "\n".join(results)

    except Exception as e:
        return f"搜索地点时发生错误: {str(e)}"


# tools.py（新增路径规划工具）

async def get_route(
    origin: str,
    destination: str,
    route_type: str = "driving"
) -> str:
    """
    获取两点之间的路线规划信息（支持驾车/步行/公交/骑行）
    
    :param origin: 起点（城市+地点，如"武汉黄鹤楼"）
    :param destination: 终点（城市+地点，如"武汉东湖"）
    :param route_type: 路线类型 ("driving", "walking", "transit", "bicycling")
    :return: 格式化的路线摘要
    """
    import httpx
    from urllib.parse import quote
    
    # 1. 先用地理编码把起点/终点转成经纬度
    async def geocode(address: str) -> tuple:
        url = "https://restapi.amap.com/v3/geocode/geo"
        params = {
            "key": GAODE_API_KEY,
            "address": address,
            "output": "json"
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10.0)
            data = resp.json()
            if data["status"] == "1" and data["geocodes"]:
                loc = data["geocodes"][0]["location"]  # "lng,lat"
                lng, lat = loc.split(",")
                return (float(lng), float(lat))
            raise ValueError(f"无法解析地址: {address}")
    
    try:
        # 2. 获取起点和终点的经纬度
        origin_lng, origin_lat = await geocode(origin)
        dest_lng, dest_lat = await geocode(destination)
        
        # 3. 构建路径规划请求
        route_apis = {
            "driving": "https://restapi.amap.com/v3/direction/driving",
            "walking": "https://restapi.amap.com/v3/direction/walking",
            "bicycling": "https://restapi.amap.com/v4/direction/bicycling",
            "transit": "https://restapi.amap.com/v3/direction/transit/integrated"
        }
        
        if route_type not in route_apis:
            route_type = "driving"
        
        url = route_apis[route_type]
        params = {
            "key": GAODE_API_KEY,
            "origin": f"{origin_lng},{origin_lat}",
            "destination": f"{dest_lng},{dest_lat}",
            "output": "json"
        }
        
        # 公交需要额外参数
        if route_type == "transit":
            # 尝试从地址中提取城市（简化处理）
            city = origin.split("市")[0] if "市" in origin else "北京"
            params["city"] = city
        
        # 4. 调用路径规划API
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=15.0)
            data = resp.json()
            
            if data["status"] != "1":
                return f"路径规划失败: {data.get('info', '未知错误')}"
            
            # 5. 解析不同类型的返回结果
            if route_type == "driving":
                path = data["route"]["paths"][0]
                distance = int(path["distance"]) / 1000  # 米 → 公里
                duration = int(path["duration"]) / 60    # 秒 → 分钟
                tolls = path.get("tolls", 0)
                return f"🚗 驾车路线：约{distance:.1f}公里，预计{duration:.0f}分钟，过路费{tolls}元。"
                
            elif route_type == "walking":
                path = data["route"]["paths"][0]
                distance = int(path["distance"]) / 1000
                duration = int(path["duration"]) / 60
                return f"🚶 步行路线：约{distance:.1f}公里，预计{duration:.0f}分钟。"
                
            elif route_type == "bicycling":
                path = data["data"]["paths"][0]
                distance = int(path["distance"]) / 1000
                duration = int(path["duration"]) / 60
                return f"🚲 骑行路线：约{distance:.1f}公里，预计{duration:.0f}分钟。"
                
            elif route_type == "transit":
                if not data["route"]["transits"]:
                    return "未找到公交方案。"
                transit = data["route"]["transits"][0]
                distance = int(transit["distance"]) / 1000
                duration = int(transit["duration"]) / 60
                cost = transit.get("cost", "未知")
                walking = int(transit["walking_distance"]) / 1000
                return f"🚌 公交路线：约{distance:.1f}公里，预计{duration:.0f}分钟，费用{cost}元，步行{walking:.1f}公里。"
                
    except Exception as e:
        return f"路线查询出错: {str(e)}"
#tools 列表(给ai看的)

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "根据城市名称（支持中文，如北京、武汉）获取实时天气信息，包括天气状况和温度",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string", "description": "城市名称，如'北京'"}},
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_places",
            "description": "在指定城市搜索景点、餐厅、酒店等地点，并返回名称、地址和评分",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，如'黄鹤楼'、'火锅'、'五星级酒店'"},
                    "city": {"type": "string", "description": "城市名称，如'武汉'"}
                },
                "required": ["keyword", "city"]
            }
        }
    },
    {
    "type": "function",
    "function": {
        "name": "get_route",
        "description": "获取两个地点之间的出行路线规划（支持驾车、步行、公交、骑行），返回距离、时间和费用等摘要信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "起点，格式如'武汉黄鹤楼'"},
                "destination": {"type": "string", "description": "终点，格式如'武汉东湖'"},
                "route_type": {
                    "type": "string",
                    "description": "出行方式",
                    "enum": ["driving", "walking", "transit", "bicycling"],
                    "default": "driving"
                              }
                          },
            "required": ["origin", "destination"]
                       }
                }
    }
    
]
available_functions = {
    "get_weather": get_weather,
    "search_places": search_places,
    "get_route":get_route
}