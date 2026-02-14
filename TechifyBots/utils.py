import aiohttp
from vars import *
import re

async def get_shortlink(link: str, is_second: bool = False, is_third: bool = False) -> str:
    """
    Generate short link using appropriate shortener based on verification stage
    """
    try:
        # Select appropriate shortener based on verification stage
        if is_third:
            api = SHORTENER_API3
            website = SHORTENER_WEBSITE3
        elif is_second:
            api = SHORTENER_API2
            website = SHORTENER_WEBSITE2
        else:
            api = SHORTENER_API
            website = SHORTENER_WEBSITE
        
        url = f'https://{website}/api'
        params = {'api': api, 'url': link}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, raise_for_status=True) as response:
                data = await response.json()
                if data["status"] == "success":
                    return data['shortenedUrl']
                else:
                    print(f"Shortener Error: {data}")
                    return link
    except Exception as e:
        print(f"Error in get_shortlink: {e}")
        return link

def get_readable_time(seconds: int) -> str:
    """Convert seconds to readable time format"""
    count = 0
    readable_time = ""
    time_list = []
    time_suffix_list = ["s", "m", "h", "d"]
    
    while count < 4:
        count += 1
        remainder, result = divmod(seconds, 60) if count < 3 else divmod(seconds, 24)
        if seconds == 0 and remainder == 0:
            break
        time_list.append(int(result))
        seconds = int(remainder)
    
    for x in range(len(time_list)):
        time_list[x] = str(time_list[x]) + time_suffix_list[x]
    
    if len(time_list) == 4:
        readable_time += f"{time_list.pop()}, "
    
    time_list.reverse()
    readable_time += ":".join(time_list)
    
    return readable_time

def extract_user_id_from_start(text: str) -> tuple[str, str, str, str]:
    """
    Extract user_id, verify_id, and video_id from verification callback
    Returns: (action, user_id, verify_id, video_id)
    """
    try:
        parts = text.split("_")
        if len(parts) >= 4:
            return parts[0], parts[1], parts[2], parts[3]
        return "", "", "", ""
    except:
        return "", "", "", ""
