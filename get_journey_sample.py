import asyncio
import httpx
import json

async def main():
    async with httpx.AsyncClient(verify=False) as client:
        r = await client.get("https://v0.ovapi.nl/journey/")
        keys = [k for k in r.json().keys() if k.startswith("RET_")][:1]
        if keys:
            r = await client.get(f"https://v0.ovapi.nl/journey/{keys[0]}")
            print(json.dumps(r.json(), indent=2))

asyncio.run(main())
